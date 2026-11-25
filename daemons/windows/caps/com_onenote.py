"""capability: cap:com.onenote  (Windows, via the OneNote desktop COM API).

Lets the environment-agnostic office/onenote app read notebooks/sections/pages
and page content from the LOCAL OneNote desktop app — no browser token needed.
Graph (token) is the app's primary path; this COM cap is the FALLBACK (and the
only path when offline / not signed into a web app).

OneNote's COM model is XML-based:
  Application.GetHierarchy(bstrStartNode, hsScope, out bstrXml)
      hsScope: 0=Notebooks 1=Sections 2=Pages 4=Children  -> the hierarchy XML
  Application.GetPageContent(bstrPageId, out bstrXml)       -> a page's content XML
We parse the XML (namespace-agnostic by local-tag) into the SAME shapes the app
returns from Graph, so the app layer is backend-neutral.

win32com/pythoncom confined to caps/. Each invoke runs on a worker thread with
its own COM apartment (the daemon dispatches that way); we CoInitialize per call
and cache the Application handle thread-local.

Methods (def fn(args, ctx) -> dict):
  status()                         -> {ok, available}
  notebooks()                      -> {ok, notebooks:[{id,name,...}]}
  sections(notebook?)              -> {ok, sections:[{id,name,notebook,notebook_id}]}
  pages(section?, top?, query?)    -> {ok, pages:[{id,title,section,section_id,...}]}
  page(id)                         -> {ok, id, text}   (plain text from content XML)
"""
from __future__ import annotations

import html as _html
import re as _re
import threading as _threading

_tls = _threading.local()

# OneNote hierarchy scopes
_HS_NOTEBOOKS = 0
_HS_SECTIONS = 1
_HS_PAGES = 2
_HS_CHILDREN = 4


def _app():
    """Thread-local OneNote.Application handle. OneNote exposes its automation on
    a DUAL interface (Application2) whose methods late-binding (dynamic Dispatch)
    often cannot reach, and whose typelib frequently refuses makepy — so we probe
    a REAL method and surface a clean failure instead of a false-positive handle."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app = getattr(_tls, "app", None)
    if app is not None:
        return app
    try:
        app = win32com.client.Dispatch("OneNote.Application")
        # Probe the ACTUAL method we need; if late-binding can't reach it, this
        # raises and we report unavailable (don't cache a dead handle).
        app.GetHierarchy("", _HS_NOTEBOOKS)
    except Exception as exc:  # noqa: BLE001
        _tls.app = None
        raise RuntimeError("OneNote COM automation unavailable (%s); use Graph"
                           % (str(exc).split("\n")[0][:120])) from None
    _tls.app = app
    return app


def _get_hierarchy(start, scope):
    """Call GetHierarchy and return the XML string (handles the out-param form
    pywin32 exposes as a return value)."""
    app = _app()
    xml = app.GetHierarchy(start or "", scope)
    return xml or ""


# --- tiny namespace-agnostic XML walk (OneNote XML has a ns prefix) ---------
def _localname(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_xml(xml):
    import xml.etree.ElementTree as _ET
    try:
        return _ET.fromstring(xml)
    except Exception:  # noqa: BLE001
        return None


def _iter(elem, name):
    """Yield descendants whose local tag == name."""
    for e in elem.iter():
        if _localname(e.tag) == name:
            yield e


def status(args, ctx):
    try:
        _app()
        return {"ok": True, "available": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "available": False, "detail": str(exc)[:200]}


def notebooks(args, ctx):
    try:
        xml = _get_hierarchy("", _HS_NOTEBOOKS)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}
    root = _parse_xml(xml)
    out = []
    if root is not None:
        for nb in _iter(root, "Notebook"):
            out.append({"id": nb.get("ID", ""), "name": nb.get("name", ""),
                        "is_default": (nb.get("isCurrentlyViewed") == "true"),
                        "modified": nb.get("lastModifiedTime", ""),
                        "web_url": ""})
    return {"ok": True, "count": len(out), "notebooks": out}


def sections(args, ctx):
    """All sections (scope=Sections from each notebook), annotated with notebook.
    Optional notebook (id or name) narrows it."""
    want = (args.get("notebook") or args.get("notebook_id") or "").strip().lower()
    try:
        xml = _get_hierarchy("", _HS_SECTIONS)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}
    root = _parse_xml(xml)
    out = []
    if root is not None:
        for nb in _iter(root, "Notebook"):
            nb_name = nb.get("name", "")
            nb_id = nb.get("ID", "")
            if want and want not in (nb_name.lower(), nb_id.lower()):
                continue
            for sec in _iter(nb, "Section"):
                out.append({"id": sec.get("ID", ""), "name": sec.get("name", ""),
                            "notebook": nb_name, "notebook_id": nb_id,
                            "modified": sec.get("lastModifiedTime", "")})
    return {"ok": True, "count": len(out), "sections": out}


def pages(args, ctx):
    want_sec = (args.get("section") or args.get("section_id") or "").strip().lower()
    q = (args.get("query") or "").strip().lower()
    try:
        top = max(1, min(int(args.get("top") or 50), 200))
    except (TypeError, ValueError):
        top = 50
    try:
        xml = _get_hierarchy("", _HS_PAGES)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}
    root = _parse_xml(xml)
    out = []
    if root is not None:
        for nb in _iter(root, "Notebook"):
            for sec in _iter(nb, "Section"):
                sec_name, sec_id = sec.get("name", ""), sec.get("ID", "")
                if want_sec and want_sec not in (sec_name.lower(), sec_id.lower()):
                    continue
                for pg in _iter(sec, "Page"):
                    title = pg.get("name", "") or "(untitled)"
                    if q and q not in title.lower():
                        continue
                    out.append({"id": pg.get("ID", ""), "title": title,
                                "section": sec_name, "section_id": sec_id,
                                "created": pg.get("dateTime", ""),
                                "modified": pg.get("lastModifiedTime", ""),
                                "web_url": ""})
    # recent-first by modified when no section filter
    if not want_sec:
        out.sort(key=lambda p: p.get("modified", ""), reverse=True)
    return {"ok": True, "count": len(out[:top]), "pages": out[:top]}


def page(args, ctx):
    pid = (args.get("id") or args.get("page_id") or "").strip()
    if not pid:
        return {"ok": False, "error": "id required", "error_category": "application"}
    try:
        app = _app()
        xml = app.GetPageContent(pid, 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "GetPageContent failed: %s" % exc,
                "error_category": "unavailable"}
    root = _parse_xml(xml or "")
    # OneNote page content XML holds text inside <one:T> CDATA (may contain HTML).
    parts = []
    title = ""
    if root is not None:
        pg = next(_iter(root, "Page"), None)
        if pg is not None:
            title = pg.get("name", "")
        for t in _iter(root, "T"):
            frag = "".join(t.itertext())
            if frag:
                parts.append(frag)
    raw = "\n".join(parts)
    text = _html.unescape(_re.sub(r"(?s)<[^>]+>", "", raw))
    text = _re.sub(r"\n{3,}", "\n\n", text).strip()
    out = {"ok": True, "id": pid, "text": text}
    if title:
        out["title"] = title
    if args.get("html"):
        out["xml"] = xml or ""
    return out


def warmup(args, ctx):
    """Pay the OneNote COM attach cost off the first user call."""
    try:
        _app()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


METHODS = {"status": status, "notebooks": notebooks, "sections": sections,
           "pages": pages, "page": page, "warmup": warmup}
CAPABILITY = "cap:com.onenote"
VARIANT = None
ATTRS = {}
