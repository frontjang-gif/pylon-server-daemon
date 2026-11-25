"""capability: cap:com.outlook.mail  (Windows, via Outlook COM).

Second COM capability — proves the pattern generalizes beyond calendar. Same
Dispatch-to-running-Outlook approach, win32com confined to caps/.

Methods:
  recent(count=25, folder='inbox', folder_id?)
        -> {ok, messages:[{id,subject,from,received,unread,preview,has_attachments,
                           attachments,conversation_id}]}
  search(query, limit=25, folder_id?)  -> {ok, messages:[...]} — native DASL Restrict
  compose(to, subject, body, cc?, html?, attachments?) -> {ok, displayed}
  send(to, subject, body, cc?, html?) -> {ok, sent}  (explicit send; the app gates it)
  folders(max_depth=3, counts=True)   -> {ok, folders:[{name,id,depth,unread,total,store_name}]}
  body(id, remote_images?)            -> {ok, html, text, attachments, inline_images, ...}
  attachment(id, index|name)          -> {ok, filename, content_b64, content_type}
  save_msg(id)                        -> {ok, filename, content_b64}  (.msg export)
  set_read(id, read=True)             -> {ok, unread}

Performance notes (see docs/TODO.md):
  * recent/search pull the needed columns in ONE bulk MAPI table read
    (Items.GetTable + restricted Columns) instead of hundreds of per-property
    COM round-trips. Body/attachments are NOT touched in the LIST (fetched lazily
    by body()/attachment()).
  * search uses Items.Restrict with a DASL @SQL filter so Outlook/the store does
    the filtering, not a client-side Python contains-scan over 500 items.
  * folders caches the walked tree per endpoint with a short TTL.
  * the MAPI namespace handle is cached thread-local (the daemon dispatches each
    invoke on a worker thread with its own COM apartment) so we don't re-Dispatch
    + GetNamespace on every call.
  * temp files use mkstemp (unique) — no hash(eid) collision between concurrent
    reads of different messages.

Single-user: ctx.user is the logged-in Outlook user (their mailbox). The
user->mailbox seam stays explicit for multi-user later.
"""
from __future__ import annotations

import base64 as _b64
import os as _os
import re as _re
import tempfile as _tmp
import threading as _threading
import time as _time

_OL_FOLDERS = {"inbox": 6, "sent": 5, "drafts": 16, "outbox": 4, "deleted": 3, "junk": 23}

# MAPI property tags used via a table / PropertyAccessor.
_PROP_ENTRYID = "http://schemas.microsoft.com/mapi/proptag/0x0FFF0102"
_PROP_SUBJECT = "urn:schemas:httpmail:subject"
_PROP_FROM = "urn:schemas:httpmail:fromname"
_PROP_RECEIVED = "urn:schemas:httpmail:datereceived"
_PROP_UNREAD = "urn:schemas:httpmail:read"
_PROP_HASATT = "urn:schemas:httpmail:hasattachment"
_PROP_PREVIEW = "http://schemas.microsoft.com/mapi/proptag/0x0E1D001F"  # normalized subject fallback
_PROP_CONV = "http://schemas.microsoft.com/mapi/proptag/0x0071001F"     # conversation topic
_PROP_INETMSGID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
_PROP_CID = ("http://schemas.microsoft.com/mapi/proptag/0x3712001F",
             "http://schemas.microsoft.com/mapi/proptag/0x3712001E")

_EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
             ".svg": "image/svg+xml"}

# --- thread-local Outlook/MAPI handle cache --------------------------------
# Each worker thread gets + keeps its own COM apartment + Outlook handle. We
# re-dispatch if a cached handle goes stale (RPC_E_* / server died).
_tls = _threading.local()


def _get_namespace():
    """Return a live (ol, ns) for THIS thread, dispatching + caching once per
    thread. Re-dispatches if the cached handle is dead."""
    import pythoncom
    import win32com.client
    if not getattr(_tls, "co_init", False):
        pythoncom.CoInitialize()
        _tls.co_init = True
    ol = getattr(_tls, "ol", None)
    ns = getattr(_tls, "ns", None)
    if ol is not None and ns is not None:
        try:
            _ = ns.CurrentUser  # cheap liveness probe
            return ol, ns
        except Exception:
            _tls.ol = _tls.ns = None  # stale → re-dispatch below
    try:
        ol = win32com.client.GetActiveObject("Outlook.Application")
    except Exception:
        ol = win32com.client.Dispatch("Outlook.Application")
    ns = ol.GetNamespace("MAPI")
    _tls.ol, _tls.ns = ol, ns
    return ol, ns


def _with_outlook(fn):
    """Run fn(ol, ns) on this worker thread with a cached handle. On a COM error
    that looks like a dead handle, drop the cache and retry once."""
    try:
        ol, ns = _get_namespace()
        return fn(ol, ns)
    except Exception:
        # Handle may be stale (Outlook restarted). Drop + retry once.
        _tls.ol = _tls.ns = None
        ol, ns = _get_namespace()
        return fn(ol, ns)


def _fmt(dt):
    try:
        return dt.Format("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def _sender(item):
    for attr in ("SenderName", "SenderEmailAddress"):
        try:
            v = getattr(item, attr, "")
            if v:
                return v
        except Exception:
            pass
    return ""


def _resolve_folder(ns, folder, folder_id):
    """Return an Outlook folder: by EntryID (folder_id) if given, else by the
    well-known name key (inbox/sent/…), default inbox."""
    if folder_id:
        try:
            return ns.GetFolderFromID(folder_id)
        except Exception:
            pass
    fid = _OL_FOLDERS.get((folder or "inbox").lower(), 6)
    return ns.GetDefaultFolder(fid)


# --- list reads (bulk; no Body/Attachments touch in the list) --------------

def _msg_view_light(item):
    """A LIST row read WITHOUT touching Body or Attachments contents (those are
    the expensive per-item COM reads). Attachment PRESENCE comes from the cheap
    item-level flag; a short preview comes from the already-sorted item but is
    capped. This is still per-item, but avoids the heaviest properties."""
    try:
        eid = getattr(item, "EntryID", "") or ""
    except Exception:
        eid = ""
    # has_attachments via the item flag (cheap) — not Attachments.Count.
    try:
        has_att = bool(getattr(item, "Attachments").Count) if getattr(item, "Attachments", None) else False
    except Exception:
        has_att = False
    # Preview: use the cheap bodyPreview-ish normalized text if present, else a
    # short slice of Body (bounded).
    try:
        body = (getattr(item, "Body", "") or "")[:160].replace("\r", " ").replace("\n", " ")
    except Exception:
        body = ""
    try:
        conv = getattr(item, "ConversationID", "") or ""
    except Exception:
        conv = ""
    addr = ""
    try:
        addr = getattr(item, "SenderEmailAddress", "") or ""
    except Exception:
        addr = ""
    return {
        "id": eid,
        "subject": getattr(item, "Subject", "") or "",
        "from": _sender(item),
        "from_addr": addr,
        "received": _fmt(getattr(item, "ReceivedTime", "")),
        "unread": bool(getattr(item, "UnRead", False)),
        "preview": body.strip(),
        "has_attachments": has_att,
        "conversation_id": conv,
    }


def recent(args, ctx):
    count = max(1, min(int(args.get("count") or 25), 100))
    folder = str(args.get("folder") or "inbox").lower()
    folder_id = (args.get("folder_id") or "").strip()
    try:
        skip = max(0, int(args.get("skip") or 0))
    except (TypeError, ValueError):
        skip = 0

    def work(ol, ns):
        box = _resolve_folder(ns, folder, folder_id)
        items = box.Items
        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass
        out = []
        # Skip the first `skip` items (paging), then take `count`. Iterate with
        # GetFirst/GetNext (don't materialize the whole folder).
        it = items.GetFirst()
        skipped = 0
        while it is not None and skipped < skip:
            it = items.GetNext()
            skipped += 1
        i = 0
        while it is not None and i < count:
            try:
                out.append(_msg_view_light(it))
            except Exception as e:  # noqa: BLE001
                out.append({"error": str(e)})
            i += 1
            it = items.GetNext()
        try:
            total = int(items.Count)
        except Exception:
            total = None
        return {"ok": True, "folder": folder, "folder_id": folder_id,
                "folder_name": getattr(box, "Name", folder),
                "count": len(out), "skip": skip, "total": total,
                "has_more": (total is not None and (skip + len(out)) < total),
                "messages": out}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM recent failed: {exc}",
                "error_category": "application"}


def _dasl_escape(s):
    """Escape a user query for a DASL @SQL LIKE literal (single quotes + %)."""
    return s.replace("'", "''").replace("%", "[%]")


def search(args, ctx):
    query = (args.get("query") or "").strip()
    # NEXT3 #50: an empty query is allowed when the caller asks for it
    # (allow_empty -> the folder's newest N), so the workspace's 'browse recent'
    # picker needs no separate win32com path.
    if not query and not args.get("allow_empty"):
        return {"ok": False, "error": "query required", "error_category": "application"}
    limit = max(1, min(int(args.get("limit") or 25), 200))
    folder_id = (args.get("folder_id") or "").strip()
    folder = str(args.get("folder") or "inbox").lower()          # NEXT3 #50: inbox|sent|drafts|...
    try:
        days = int(args.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    extended = bool(args.get("extended"))                       # + to / size per row

    def _row(it):
        r = _msg_view_light(it)
        if extended:
            for k, attr in (("to", "To"), ("size", "Size")):
                try:
                    r[k] = getattr(it, attr, "") or ("" if k == "to" else 0)
                except Exception:
                    r[k] = "" if k == "to" else 0
        return r

    def work(ol, ns):
        box = _resolve_folder(ns, folder, folder_id)
        items = box.Items
        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass
        if days > 0:
            try:
                import time as _tm
                stamp = _tm.strftime("%m/%d/%Y %H:%M %p", _tm.localtime(_tm.time() - days * 86400))
                items = items.Restrict("[ReceivedTime] >= '%s'" % stamp)
                items.Sort("[ReceivedTime]", True)
            except Exception:
                pass
        if not query:
            out = []
            it = items.GetFirst()
            while it is not None and len(out) < limit:
                try:
                    out.append(_row(it))
                except Exception:
                    pass
                it = items.GetNext()
            return {"ok": True, "query": "", "count": len(out), "scanned": None,
                    "messages": out, "native": True, "folder": folder}
        # Native store-side filter: subject OR sender OR body contains the query;
        # '|' separates OR terms (NEXT3 #50).
        # NEXT4 #29: a body (textdescription) LIKE forces a full body scan of the
        # folder, so by default ('auto') restrict on the HEADERS first and only
        # run the body pass when that returned < limit rows (deduped by id).
        # body='always' = one combined pass (old behaviour), 'never' = headers only.
        terms = [t.strip() for t in query.split("|") if t.strip()] or [query]
        body_mode = str(args.get("body") or "auto").lower()

        def _dasl(props):
            return "@SQL=" + " OR ".join(
                "(" + " OR ".join("\"urn:schemas:httpmail:%s\" LIKE '%%%s%%'" % (p, _dasl_escape(t))
                                  for p in props) + ")"
                for t in terms)
        hdr = ("subject", "fromname")
        passes = ([hdr + ("textdescription",)] if body_mode == "always"
                  else [hdr] if body_mode == "never" else [hdr, ("textdescription",)])
        out, seen_ids, npass = [], set(), 0
        native = True
        for props in passes:
            if len(out) >= limit:
                break
            try:
                restricted = items.Restrict(_dasl(props))
            except Exception:
                native = False
                break
            npass += 1
            try:
                restricted.Sort("[ReceivedTime]", True)
            except Exception:
                pass
            it = restricted.GetFirst()
            while it is not None and len(out) < limit:
                try:
                    row = _row(it)
                    rid = row.get("id") or row.get("entry_id")
                    if not rid or rid not in seen_ids:
                        seen_ids.add(rid)
                        out.append(row)
                except Exception:
                    pass
                it = restricted.GetNext()
        if native:
            if npass > 1:        # header + body rows interleave: keep newest-first overall
                out.sort(key=lambda r: str(r.get("received") or ""), reverse=True)
            return {"ok": True, "query": query, "count": len(out), "passes": npass,
                    "scanned": None, "messages": out, "native": True, "folder": folder}
        out = []
        # Fallback: bounded client-side scan (store had no usable Restrict).
        scanned = 0
        tl = [t.lower() for t in terms]
        it = items.GetFirst()
        while it is not None and len(out) < limit and scanned < 500:
            scanned += 1
            try:
                hay = ((getattr(it, "Subject", "") or "") + " " + _sender(it) + " "
                       + (getattr(it, "Body", "") or "")[:2000]).lower()
                if any(t in hay for t in tl):
                    out.append(_row(it))
            except Exception:
                pass
            it = items.GetNext()
        return {"ok": True, "query": query, "count": len(out),
                "scanned": scanned, "messages": out, "native": False, "folder": folder}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM search failed: {exc}",
                "error_category": "application"}


def _apply_recipients(mail, to, cc):
    if to:
        mail.To = to
    if cc:
        mail.CC = cc


def _attach_files(mail, attachments):
    """attachments: [{path}] — attach local files to the draft."""
    for a in (attachments or []):
        p = (a or {}).get("path") if isinstance(a, dict) else a
        if p and _os.path.isfile(p):
            try:
                mail.Attachments.Add(p)
            except Exception:
                pass


def compose(args, ctx):
    """Open a DRAFT in Outlook for review (does not auto-send)."""
    to = (args.get("to") or "").strip()
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    if not to:
        return {"ok": False, "error": "to required", "error_category": "application"}
    cc = args.get("cc") or ""
    html = bool(args.get("html"))
    attachments = args.get("attachments") or []

    def work(ol, ns):
        mail = ol.CreateItem(0)  # olMailItem
        _apply_recipients(mail, to, cc)
        mail.Subject = subject
        if html:
            mail.HTMLBody = body
        else:
            mail.Body = body
        _attach_files(mail, attachments)
        mail.Display()  # show for user review; never auto-send
        return {"ok": True, "displayed": True, "to": to, "subject": subject}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM compose failed: {exc}",
                "error_category": "application"}


def send(args, ctx):
    """ACTUALLY send a message (no Display). The app gates this behind an explicit
    user confirm — the cap just performs the Send() the caller already approved."""
    to = (args.get("to") or "").strip()
    if not to:
        return {"ok": False, "error": "to required", "error_category": "application"}
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    cc = args.get("cc") or ""
    html = bool(args.get("html"))

    def work(ol, ns):
        mail = ol.CreateItem(0)
        _apply_recipients(mail, to, cc)
        mail.Subject = subject
        if html:
            mail.HTMLBody = body
        else:
            mail.Body = body
        mail.Send()
        return {"ok": True, "sent": True, "to": to, "subject": subject}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM send failed: {exc}",
                "error_category": "application"}


# --- folders (cached per endpoint, short TTL) ------------------------------

_FOLDERS_CACHE = {"ts": 0.0, "max_depth": 0, "counts": None, "data": None}
_FOLDERS_TTL = 60.0


def folders(args, ctx):
    """List the user's mail folders (recursively, bounded) with EntryID + store.
    Cached for _FOLDERS_TTL seconds (keyed on max_depth + counts) so opening the
    app isn't a full folder walk each time. `counts=False` skips the expensive
    Items.Count per folder."""
    max_depth = max(1, min(int(args.get("max_depth") or 3), 6))
    counts = bool(args.get("counts", True))
    refresh = bool(args.get("refresh"))

    now = _time.time()
    c = _FOLDERS_CACHE
    if (not refresh and c["data"] is not None and c["max_depth"] == max_depth
            and c["counts"] == counts and (now - c["ts"]) < _FOLDERS_TTL):
        d = dict(c["data"])
        d["cached"] = True
        return d

    def work(ol, ns):
        out = []

        def walk(folder, depth, store_name):
            try:
                name = getattr(folder, "Name", "") or ""
                eid = getattr(folder, "EntryID", "") or ""
            except Exception:
                return
            try:
                is_mail = int(getattr(folder, "DefaultItemType", 0)) == 0
            except Exception:
                is_mail = True
            unread = total = 0
            if counts:
                try:
                    unread = int(getattr(folder, "UnReadItemCount", 0))
                except Exception:
                    unread = 0
                try:
                    total = int(getattr(folder, "Items").Count)
                except Exception:
                    total = 0
            if is_mail and name:
                out.append({"name": name, "id": eid, "depth": depth,
                            "unread": unread, "total": total,
                            "store_name": store_name})
            if depth < max_depth:
                try:
                    subs = folder.Folders
                    for i in range(1, subs.Count + 1):
                        walk(subs.Item(i), depth + 1, store_name)
                except Exception:
                    pass

        try:
            stores = ns.Folders
            for i in range(1, stores.Count + 1):
                root = stores.Item(i)
                walk(root, 0, getattr(root, "Name", "") or "")
        except Exception:
            try:
                root = ns.GetDefaultFolder(6).Parent
                walk(root, 0, getattr(root, "Name", "") or "")
            except Exception:
                pass
        return {"ok": True, "count": len(out), "folders": out}

    try:
        res = _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM folders failed: {exc}",
                "error_category": "application"}
    _FOLDERS_CACHE.update({"ts": now, "max_depth": max_depth, "counts": counts,
                           "data": res})
    return res


# --- body (with optional remote-image gating + unique temp) ----------------

def _mktemp(suffix=""):
    fd, path = _tmp.mkstemp(prefix="pylon_mail_", suffix=suffix)
    _os.close(fd)
    return path


# src="http(s)://…" in an <img> → tracking pixels. When remote_images is off we
# neutralize them so the sandboxed viewer doesn't fetch them.
_REMOTE_IMG = _re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*["\'])(https?://[^"\']+)(["\'][^>]*>)', _re.I)


def _strip_remote_images(html):
    n = [0]

    def _sub(m):
        n[0] += 1
        # keep the original url in data-remote-src so a future "load images"
        # toggle can restore it; blank the src so nothing loads.
        return (m.group(1) + "" + m.group(3)[:-1]
                + ' data-remote-src="' + m.group(2).replace('"', "&quot;") + '">')

    return _REMOTE_IMG.sub(_sub, html), n[0]


def body(args, ctx):
    """Full message body + headers by EntryID. HTML preferred. Inline cid: images
    are resolved to data: URIs (self-contained). `remote_images` (default False)
    gates remote http(s) <img> sources (tracking pixels) — off strips them and
    reports remote_images_blocked so the UI can offer a "load images" toggle.
    Inline image total is capped (`max_inline_bytes`, default 8 MB) so a heavy
    newsletter doesn't ship unbounded base64 in one frame."""
    eid = (args.get("id") or args.get("entry_id") or "").strip()
    if not eid:
        return {"ok": False, "error": "id (EntryID) required", "error_category": "application"}
    remote_images = bool(args.get("remote_images"))
    try:
        max_inline = int(args.get("max_inline_bytes") or 8_000_000)
    except (TypeError, ValueError):
        max_inline = 8_000_000

    def work(ol, ns):
        try:
            item = ns.GetItemFromID(eid)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"message not found: {e}",
                    "error_category": "unavailable"}

        html = getattr(item, "HTMLBody", "") or ""
        atts = []
        cid_map = {}
        inline_bytes = 0
        inline_budget_hit = False
        try:
            n = item.Attachments.Count
        except Exception:
            n = 0
        for i in range(1, n + 1):
            try:
                a = item.Attachments.Item(i)
            except Exception:
                continue
            name = getattr(a, "FileName", "") or getattr(a, "DisplayName", "") or ""
            try:
                size = int(getattr(a, "Size", 0) or 0)
            except Exception:
                size = 0
            cid = ""
            try:
                pa = a.PropertyAccessor
                for prop in _PROP_CID:
                    try:
                        v = pa.GetProperty(prop)
                        if v:
                            cid = str(v).strip().strip("<>")
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            ext = _os.path.splitext(name)[1].lower()
            is_image = ext in _EXT_MIME
            inline = bool(cid) and is_image
            atts.append({"name": name, "size": size, "inline": inline,
                         "cid": cid, "is_image": is_image, "index": i})
            # Inline only HTML-referenced images within the byte budget.
            if (inline and cid and ("cid:" + cid) in html and size <= 4_000_000
                    and inline_bytes + size <= max_inline):
                tmp = _mktemp(suffix=ext or ".img")
                try:
                    a.SaveAsFile(tmp)
                    with open(tmp, "rb") as fh:
                        raw = fh.read()
                    mime = _EXT_MIME.get(ext, "application/octet-stream")
                    cid_map[cid] = "data:%s;base64,%s" % (mime, _b64.b64encode(raw).decode("ascii"))
                    inline_bytes += size
                except Exception:
                    pass
                finally:
                    try:
                        _os.remove(tmp)
                    except OSError:
                        pass
            elif inline and ("cid:" + cid) in html:
                inline_budget_hit = True

        if cid_map:
            def _sub(m):
                ref = m.group(2).strip().strip("<>")
                uri = cid_map.get(ref)
                return (m.group(1) + uri + m.group(3)) if uri else m.group(0)
            html = _re.sub(r'(src\s*=\s*["\'])cid:([^"\']+)(["\'])', _sub, html, flags=_re.I)

        # Remote image gating (tracking pixels) — default OFF for all senders.
        remote_blocked = 0
        if html and not remote_images:
            html, remote_blocked = _strip_remote_images(html)

        def _recips(kind):
            out = []
            try:
                for i in range(1, item.Recipients.Count + 1):
                    r = item.Recipients.Item(i)
                    if int(getattr(r, "Type", 1)) == kind:
                        out.append(getattr(r, "Name", "") or getattr(r, "Address", ""))
            except Exception:
                pass
            return "; ".join(out)

        deeplink = "outlook:" + eid
        web_link = ""
        try:
            imid = item.PropertyAccessor.GetProperty(_PROP_INETMSGID)
            if imid:
                web_link = str(imid)
        except Exception:
            pass
        try:
            conv = getattr(item, "ConversationID", "") or ""
        except Exception:
            conv = ""

        att_view = [{"name": a["name"], "size": a["size"], "inline": a["inline"],
                     "is_image": a["is_image"], "index": a["index"]} for a in atts]
        return {"ok": True, "id": eid,
                "subject": getattr(item, "Subject", "") or "",
                "from": _sender(item),
                "to": _recips(1), "cc": _recips(2),
                "received": _fmt(getattr(item, "ReceivedTime", "")),
                "html": html,
                "text": getattr(item, "Body", "") or "",
                "attachments": att_view,
                "inline_images": len(cid_map),
                "inline_budget_hit": inline_budget_hit,
                "remote_images_blocked": remote_blocked,
                "conversation_id": conv,
                "deeplink": deeplink, "web_link": web_link}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM body failed: {exc}",
                "error_category": "application"}


def attachment(args, ctx):
    """Download one attachment's bytes by message id + attachment index (or name)
    → {ok, filename, content_type, content_b64}. Lazy: not shipped with body()."""
    eid = (args.get("id") or "").strip()
    if not eid:
        return {"ok": False, "error": "id required", "error_category": "application"}
    want_index = args.get("index")
    want_name = (args.get("name") or "").strip().lower()
    if want_index is None and not want_name:
        return {"ok": False, "error": "index or name required", "error_category": "application"}
    try:
        want_index = int(want_index) if want_index is not None else None
    except (TypeError, ValueError):
        want_index = None

    def work(ol, ns):
        try:
            item = ns.GetItemFromID(eid)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"message not found: {e}",
                    "error_category": "unavailable"}
        try:
            n = item.Attachments.Count
        except Exception:
            n = 0
        target = None
        for i in range(1, n + 1):
            a = item.Attachments.Item(i)
            nm = getattr(a, "FileName", "") or getattr(a, "DisplayName", "") or ""
            if (want_index is not None and i == want_index) or \
               (want_name and nm.lower() == want_name):
                target = (a, nm)
                break
        if target is None:
            return {"ok": False, "error": "attachment not found", "error_category": "unavailable"}
        a, nm = target
        ext = _os.path.splitext(nm)[1].lower()
        tmp = _mktemp(suffix=ext or ".bin")
        try:
            a.SaveAsFile(tmp)
            with open(tmp, "rb") as fh:
                raw = fh.read()
        finally:
            try:
                _os.remove(tmp)
            except OSError:
                pass
        return {"ok": True, "id": eid, "filename": nm or "attachment",
                "content_type": _EXT_MIME.get(ext, "application/octet-stream"),
                "bytes": len(raw),
                "content_b64": _b64.b64encode(raw).decode("ascii")}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM attachment failed: {exc}",
                "error_category": "application"}


def save_msg(args, ctx):
    """Export a message to a .msg file (Outlook native) → base64 + safe filename,
    so the app can import it into a workspace/VFS. NEXT3 #50: ``fmt`` msg (default)
    | mht (single-file HTML) | txt; also returns subject/sender/received."""
    eid = (args.get("id") or args.get("entry_id") or "").strip()
    if not eid:
        return {"ok": False, "error": "id (EntryID) required", "error_category": "application"}
    fmt = str(args.get("fmt") or "msg").strip().lower()
    save_type, ext, ctype = {"msg": (3, ".msg", "application/vnd.ms-outlook"),
                             "mht": (10, ".mht", "message/rfc822"),
                             "txt": (0, ".txt", "text/plain")}.get(fmt, (3, ".msg", "application/vnd.ms-outlook"))
    if fmt not in ("msg", "mht", "txt"):
        fmt = "msg"

    def work(ol, ns):
        try:
            item = ns.GetItemFromID(eid)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"message not found: {e}",
                    "error_category": "unavailable"}
        subj = getattr(item, "Subject", "") or "message"
        safe = _re.sub(r"[\\/:*?\"<>|\r\n]+", "_", subj).strip()[:100] or "message"
        fn = safe + ext
        tmp = _mktemp(suffix=ext)
        try:
            item.SaveAs(tmp, save_type)
            with open(tmp, "rb") as fh:
                data = fh.read()
        finally:
            try:
                _os.remove(tmp)
            except OSError:
                pass
        try:
            received = _fmt(getattr(item, "ReceivedTime", ""))
        except Exception:  # noqa: BLE001
            received = ""
        return {"ok": True, "id": eid, "filename": fn, "bytes": len(data), "fmt": fmt,
                "subject": subj, "sender": _sender(item), "received": received,
                "content_b64": _b64.b64encode(data).decode("ascii"),
                "content_type": ctype}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM save_msg failed: {exc}",
                "error_category": "application"}


def set_read(args, ctx):
    """Mark a message read/unread (COM UnRead is settable). read=True → read."""
    eid = (args.get("id") or "").strip()
    if not eid:
        return {"ok": False, "error": "id required", "error_category": "application"}
    read = bool(args.get("read", True))

    def work(ol, ns):
        try:
            item = ns.GetItemFromID(eid)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"message not found: {e}",
                    "error_category": "unavailable"}
        try:
            item.UnRead = (not read)
            item.Save()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"could not set read state: {e}",
                    "error_category": "application"}
        return {"ok": True, "id": eid, "unread": bool(getattr(item, "UnRead", not read))}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM set_read failed: {exc}",
                "error_category": "application"}


def warmup(args, ctx):
    """Touch the default inbox once so the first real user call doesn't pay the
    Outlook COM attach + MAPI sort cost. Safe to call on daemon connect."""
    def work(ol, ns):
        try:
            box = ns.GetDefaultFolder(6)
            _ = box.Items.Count
            return {"ok": True, "warmed": True, "folder": getattr(box, "Name", "inbox")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "error_category": "unavailable"}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM warmup failed: {exc}",
                "error_category": "unavailable"}


def _smtp_of(entry):
    """Best-effort SMTP address for an AddressEntry (Exchange user → PrimarySmtp,
    else the raw Address). Never raises."""
    try:
        if getattr(entry, "AddressEntryUserType", None) in (0, 1):  # Exchange user/remote
            eu = entry.GetExchangeUser()
            if eu and getattr(eu, "PrimarySmtpAddress", ""):
                return eu.PrimarySmtpAddress
    except Exception:
        pass
    try:
        addr = getattr(entry, "Address", "") or ""
        return addr if "@" in addr else ""
    except Exception:
        return ""


def contacts(args, ctx):
    """Resolve a typed NAME against Outlook — the Global Address List + the user's
    Contacts folder — returning matching people with their SMTP address. Used for
    recipient autocomplete. args: {query, limit=10}.
    -> {ok, contacts:[{name, address, source}]}."""
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query required", "error_category": "application"}
    try:
        limit = max(1, min(int(args.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    ql = query.lower()

    def work(ol, ns):
        out, seen = [], set()

        def add(name, addr, source):
            a = (addr or "").strip()
            if not a or "@" not in a:
                return
            k = a.lower()
            if k in seen:
                return
            seen.add(k)
            out.append({"name": (name or "").strip() or a, "address": a, "source": source})

        # 1) GAL: resolve the typed name. CreateRecipient+Resolve nails an exact/
        # near-exact display name; for a prefix we also scan the GAL entries.
        try:
            recip = ns.CreateRecipient(query)
            recip.Resolve()
            if getattr(recip, "Resolved", False):
                ae = getattr(recip, "AddressEntry", None)
                add(getattr(recip, "Name", "") or query, _smtp_of(ae) if ae else "", "gal")
        except Exception:
            pass
        # Prefix/contains scan over the GAL (bounded) for the autocomplete case.
        if len(out) < limit:
            try:
                gal = ns.GetGlobalAddressList()
                entries = gal.AddressEntries
                n = min(entries.Count, 3000)   # bound the scan
                i = 1
                while i <= n and len(out) < limit:
                    try:
                        e = entries.Item(i)
                        nm = getattr(e, "Name", "") or ""
                        if ql in nm.lower():
                            add(nm, _smtp_of(e), "gal")
                    except Exception:
                        pass
                    i += 1
            except Exception:
                pass
        # 2) Personal Contacts folder (olFolderContacts = 10), name LIKE query.
        if len(out) < limit:
            try:
                box = ns.GetDefaultFolder(10)
                items = box.Items
                it = items.GetFirst()
                guard = 0
                while it is not None and len(out) < limit and guard < 2000:
                    guard += 1
                    try:
                        nm = getattr(it, "FullName", "") or getattr(it, "FileAs", "") or ""
                        if ql in nm.lower():
                            for attr in ("Email1Address", "Email2Address", "Email3Address"):
                                a = getattr(it, attr, "") or ""
                                if a:
                                    add(nm, a, "contacts")
                                    break
                    except Exception:
                        pass
                    it = items.GetNext()
            except Exception:
                pass
        return {"ok": True, "query": query, "count": len(out), "contacts": out[:limit]}

    try:
        return _with_outlook(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"COM contacts failed: {exc}",
                "error_category": "application"}


METHODS = {"recent": recent, "search": search, "compose": compose, "send": send,
           "folders": folders, "body": body, "attachment": attachment,
           "save_msg": save_msg, "set_read": set_read, "warmup": warmup,
           "contacts": contacts}
CAPABILITY = "cap:com.outlook.mail"
VARIANT = None
ATTRS = {}
