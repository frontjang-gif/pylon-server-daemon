"""capability: cap:integrations.teams  (Windows, via the signed-in Edge + CDP).

Endpoint-bound: fetches Microsoft Teams data (conversations + messages) from the
Teams NG messaging API, authenticated by the user's ALREADY-SIGNED-IN Edge
session — no stored secret in the daemon. Ported/rescoped from ai-pylon
apps/integrations/teams: in ai-pylon2 teams FETCHES ONLY (no command dispatch,
no knowledge/LLM, no workflows) and gets its auth from the browser session
(credential/connection + cdp), per the ai-pylon2 scope.

How: the Teams NG API (``*.ng.msg.teams.microsoft.com``) is same-origin to the
``teams.microsoft.com`` web app, which already holds the signed-in session. So
we run the API ``fetch()`` INSIDE a Teams tab over CDP — the browser attaches
the skypetoken/cookies automatically; we never handle the token ourselves. If no
Teams tab is open we stage one (headless managed Edge) and close it after.

Methods:
  conversations(page_size=20)        -> {ok, conversations:[...]}
  messages(conversation_id, page_size=20) -> {ok, messages:[...]}
  status()                           -> {ok, signed_in, region}

Degrades to {ok:False, unavailable} if no signed-in Teams session is reachable.
"""
from __future__ import annotations

import json
import os as _os
import time

from . import _cdp_common as cdp

try:
    from . import _edge_profile as _edge
except Exception:  # noqa: BLE001
    _edge = None

_TEAMS_URL = "https://teams.cloud.microsoft/v2/"
_NG_BASE = "https://teams.microsoft.com"  # same-origin proxy base is fine; NG host also works
# The real NG host the app talks to (region-specific). We call it same-origin
# from the teams.microsoft.com tab, which the browser authorizes via its session.
_NG_HOST = "https://emea.ng.msg.teams.microsoft.com"


def _ensure_endpoint():
    # Reuse whatever managed Edge is already running, in WHATEVER mode — never
    # force a mode flip (which kills + relaunches the shared browser and tears
    # down any live Teams session, stalling sync / sends). Only launch when
    # nothing is up. (Reading/fetching inside a Teams tab works headed or
    # headless; the headed latch from the sync path is respected by ensure_running.)
    if _edge is None:
        return cdp.ensure_available()
    dbg = None
    try:
        if _edge.current_mode() is not None:
            p = int(open(_edge.PORT_FILE).read().strip())
            dbg = "http://127.0.0.1:%d" % p
    except Exception:  # noqa: BLE001
        dbg = None
    if not dbg:
        dbg = _edge.ensure_running(headless=True)
    if not dbg:
        return cdp.ensure_available()
    cdp.configure(dbg)
    return cdp.ensure_available()


def _teams_tab():
    """Return an open Teams page tab with a debugger WS, or None. Matches both
    the v2 host (teams.cloud.microsoft) and the legacy teams.microsoft.com."""
    for t in cdp.page_tabs():
        url = (t.get("url") or "").lower()
        if ("teams.cloud.microsoft" in url or "teams.microsoft.com" in url) \
                and t.get("webSocketDebuggerUrl"):
            return t
    return None


def _with_teams_tab(fn):
    """Run fn(target) on a Teams tab, staging+closing one if none is open."""
    bad = _ensure_endpoint()
    if bad:
        return bad
    tab = _teams_tab()
    if tab is not None:
        return fn(tab)
    o = cdp.open_tab(_TEAMS_URL)
    opened = o.get("id")
    try:
        cdp.wait_for_ws(opened, timeout=12)
        time.sleep(5)  # let the SPA boot + the session settle
        tgt = cdp.target_by_id(opened)
        if not tgt:
            return {"ok": False, "error": "could not open a Teams tab",
                    "error_category": "unavailable"}
        return fn(tgt)
    finally:
        if opened:
            cdp.close_tab(opened)


def _fetch_json(target, url):
    """Do an authenticated same-origin fetch() inside the Teams tab over CDP and
    return (data, err). The browser attaches the session auth automatically."""
    # async IIFE returning a JSON string; eval with awaitPromise semantics via a
    # polled global (eval_on_target_pooled does not awaitPromise, so stash the
    # result on window and poll it).
    token = "__pylon_teams_%d" % int(time.time() * 1000)
    kick = (
        "(function(){window.%s=null;"
        "fetch(%s,{credentials:'include',headers:{'Accept':'application/json'}})"
        ".then(r=>r.text()).then(t=>{window.%s=JSON.stringify({status:200,body:t});})"
        ".catch(e=>{window.%s=JSON.stringify({status:0,error:String(e)});});"
        "return 'kicked';})()" % (token, json.dumps(url), token, token)
    )
    r = cdp.eval_on_target_pooled(target, kick, timeout=15)
    if not r.get("ok"):
        return None, {"ok": False, "error": "cdp eval failed: %s" % r.get("error"),
                      "error_category": "application"}
    deadline = time.time() + 25
    while time.time() < deadline:
        poll = cdp.eval_on_target_pooled(target, "window.%s" % token, timeout=10)
        val = poll.get("value") if poll.get("ok") else None
        if val:
            try:
                env = json.loads(val)
            except (TypeError, ValueError):
                return None, {"ok": False, "error": "bad fetch envelope",
                              "error_category": "application"}
            if env.get("error"):
                return None, {"ok": False, "error": "teams fetch failed: %s" % env["error"],
                              "error_category": "unavailable"}
            try:
                return json.loads(env.get("body") or "{}"), None
            except (TypeError, ValueError):
                return {"_raw": env.get("body")}, None
        time.sleep(0.4)
    return None, {"ok": False, "error": "teams fetch timed out",
                  "error_category": "unavailable"}


def conversations(args, ctx):
    try:
        page_size = max(1, min(50, int(args.get("page_size", 20) or 20)))
    except (TypeError, ValueError):
        page_size = 20

    def _run(target):
        url = ("%s/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=%d"
               % (_NG_HOST, page_size))
        data, err = _fetch_json(target, url)
        if err:
            return err
        # Distinguish "signed in, 0 chats" from "not signed in". The NG API, when
        # the session is valid, returns an object WITH a 'conversations' key (even
        # if the array is empty). No such key (login redirect / error body) =>
        # not authenticated — tell the caller so the UI can prompt a sign-in.
        signed_in = isinstance(data, dict) and ("conversations" in data)
        convs = []
        for c in (data.get("conversations") or []):
            last = c.get("lastMessage") or {}
            convs.append({
                "id": c.get("id", ""),
                "topic": (c.get("threadProperties") or {}).get("topic", ""),
                "last_message_ts": last.get("originalarrivaltime", ""),
                "last_sender": last.get("imdisplayname", ""),
                "last_preview": (last.get("content", "") or "")[:200],
                "messagetype": last.get("messagetype", ""),
            })
        return {"ok": True, "signed_in": signed_in, "count": len(convs),
                "conversations": convs}

    return _with_teams_tab(_run)


# JS (run in the signed-in Teams tab) that reads the SPA's own IndexedDB
# conversation-manager store — the chat list the SPA has ALREADY synced over its
# real-time channel, including 1:1 (@unq.gbl.spaces) chats that no REST endpoint
# returns to us (the chat token is session-bound; see investigation notes). We
# read it locally — no chatsvc auth needed. Returns normalized conversation rows.
_LOCAL_CONV_JS = r"""
(async()=>{
 function openDB(n){return new Promise((res,rej)=>{var r=indexedDB.open(n);r.onsuccess=()=>res(r.result);r.onerror=()=>rej(r.error);});}
 function getAll(store,db){return new Promise((res)=>{try{var tx=db.transaction(store,'readonly');var rq=tx.objectStore(store).getAll();rq.onsuccess=()=>res(rq.result||[]);rq.onerror=()=>res([]);}catch(e){res([]);}});}
 try{
  var dbs=await indexedDB.databases();
  // Scan EVERY conversation-manager DB (there can be more than one) and keep the
  // FRESHEST record per conversation id, so a stale DB can't mask fresh data.
  var names=(dbs.map(d=>d.name).filter(Boolean)).filter(n=>n.indexOf('conversation-manager')>=0);
  if(!names.length) return JSON.stringify({ok:false,error:'no conversation-manager db'});
  var best={};
  for(var di=0;di<names.length;di++){
    try{
      var db=await openDB(names[di]); var rows=await getAll('conversations',db); db.close();
      for(var i=0;i<rows.length;i++){
        var c=rows[i]||{}; var id=c.id||''; if(!id) continue;
        var ts=c.lastMessageTimeUtc||0;
        if(!best[id] || ts>(best[id].lastMessageTimeUtc||0)) best[id]=c;
      }
    }catch(e){}
  }
  var out=[];
  for(var id in best){
    var c=best[id]; var tp=c.threadProperties||{}; var lm=c.lastMessage||{};
    var isMeeting = id.indexOf('19:meeting')===0;
    var isOne = id.indexOf('@unq.gbl.spaces')>=0 || (tp.threadType==='chat' && (c.members||[]).length<=2 && !tp.topic);
    var kind = isMeeting?'meeting':(isOne?'oneonone':'group');
    var title = (c.chatTitle&&(c.chatTitle.longTitle||c.chatTitle.shortTitle)) || tp.topic || '';
    if(!title){
      var names2=[];
      try{ (c.chatTitle&&c.chatTitle.avatarUsersInfo||[]).forEach(function(u){ if(u.displayName) names2.push(u.displayName); }); }catch(e){}
      if(!names2.length && lm.imdisplayname) names2.push(lm.imdisplayname);
      title = names2.join(', ');
    }
    out.push({
      id:id, kind:kind, topic:title,
      last_message_ts: lm.originalarrivaltime || (c.lastMessageTimeUtc? new Date(c.lastMessageTimeUtc).toISOString():''),
      last_sender: lm.imdisplayname||'',
      last_preview: (lm.preview||lm.content||'').replace(/<[^>]+>/g,'').slice(0,200),
      messagetype: lm.messagetype||'',
      members: (c.members||[]).map(m=>m.id),
      hidden: !!tp.hidden,
      is_read: tp.isRead!==false
    });
  }
  return JSON.stringify({ok:true, dbs:names, count:out.length, conversations:out});
 }catch(e){return JSON.stringify({ok:false,error:String(e)});}
})()
"""


def _all_edge_ports():
    """Debuggable Edge instances to scan for a synced Teams session. Includes the
    daemon's managed Edge AND any other local debuggable Edge (e.g. the user's
    long-running ai-pylon profile on :65299) — we read whichever has the FRESHEST
    chat sync, so stale/headless instances can't mask current 1:1 data."""
    import urllib.request as _u
    ports = set()
    # the daemon's managed Edge (from its port sidecar / current cdp config)
    try:
        if _edge is not None and _edge.PORT_FILE and _os.path.isfile(_edge.PORT_FILE):
            ports.add(int(open(_edge.PORT_FILE).read().strip()))
    except Exception:  # noqa: BLE001
        pass
    # common/known debuggable Edge ports used on this host
    for p in (65299, 9222, 9333):
        ports.add(p)
    live = []
    for p in ports:
        try:
            _u.urlopen("http://127.0.0.1:%d/json/version" % p, timeout=2).read()
            live.append(p)
        except Exception:  # noqa: BLE001
            pass
    return live


def _teams_tabs_on(port):
    import json as _json
    import urllib.request as _u
    try:
        data = _json.loads(_u.urlopen("http://127.0.0.1:%d/json" % port, timeout=5).read())
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in data:
        url = (t.get("url") or "").lower()
        if t.get("type") == "page" and ("teams.cloud.microsoft" in url
                                        or "teams.microsoft.com" in url) \
                and t.get("webSocketDebuggerUrl"):
            out.append(t)
    return out


def local_conversations(args, ctx):
    """Return the Teams SPA's locally-synced conversation list (incl. 1:1 chats),
    read from its IndexedDB. Scans ALL reachable debuggable Edge instances for a
    signed-in Teams tab and merges the conversation records keeping the FRESHEST
    per id — so we pick up current 1:1s from whichever Edge actually has an
    up-to-date sync (a long-running signed-in profile), not a stale/headless one.

    If none is reachable, falls back to ensuring/launching a HEADED managed Edge
    (headless throttles Teams' sync) with a Teams tab, left open to sync."""
    import json as _json
    import time as _t

    merged = {}          # id -> record (freshest wins)
    sources = []

    def _read_tab(target):
        r = cdp.eval_on_target_pooled(target, _LOCAL_CONV_JS.strip(), timeout=40)
        if not r.get("ok"):
            return None
        try:
            return _json.loads(r.get("value") or "{}")
        except (TypeError, ValueError):
            return None

    def _absorb(payload):
        if not (isinstance(payload, dict) and payload.get("ok")):
            return 0
        n = 0
        for c in payload.get("conversations") or []:
            cid = c.get("id")
            if not cid:
                continue
            ts = c.get("last_message_ts") or ""
            prev = merged.get(cid)
            if prev is None or ts > (prev.get("last_message_ts") or ""):
                merged[cid] = c
                n += 1
        return n

    # 1) scan every reachable debuggable Edge for a Teams tab + read its IDB.
    for port in _all_edge_ports():
        for tab in _teams_tabs_on(port):
            pay = _read_tab(tab)
            got = _absorb(pay)
            if got:
                sources.append("port %d (%d)" % (port, got))

    # 2) if nothing found, ensure a HEADED managed Edge + Teams tab, read it.
    if not merged:
        if _edge is not None:
            try:
                dbg = _edge.ensure_mode(headless=False)
                if dbg:
                    cdp.configure(dbg)
            except Exception:  # noqa: BLE001
                pass
        cdp.ensure_available()
        tab = _teams_tab()
        if tab is None:
            o = cdp.open_tab(_TEAMS_URL)
            opened = o.get("id")
            cdp.wait_for_ws(opened, timeout=12)
            _t.sleep(10)
            tab = cdp.target_by_id(opened)
        if tab:
            _absorb(_read_tab(tab))

    if not merged:
        return {"ok": False, "error": "no signed-in Teams tab with synced chats "
                "(open Teams in the managed browser)", "error_category": "unavailable"}
    convs = list(merged.values())
    return {"ok": True, "count": len(convs), "sources": sources, "conversations": convs}


def fetch(args, ctx):
    """Generic authenticated SAME-ORIGIN GET run inside the signed-in Teams tab.
    Any teams.microsoft.com-origin URL (NG messaging, the CSA chat aggregator,
    Graph-via-Teams, …) is fetched with the SPA's own session attached by the
    browser — so endpoints that reject a bare ic3.teams bearer (e.g. CSA 1:1
    chats) succeed. Returns {ok, status, body} like credential.fetch so the
    credential app can proxy it transparently."""
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url required", "error_category": "application"}

    def _run(target):
        data, err = _fetch_json(target, url)
        if err:
            # surface a 401-ish signal so callers can fall back
            return {"ok": False, "status": 0, "error": err.get("error", ""),
                    "error_category": err.get("error_category", "unavailable")}
        # _fetch_json already json.loads()'d the body; re-serialize so the
        # credential-app contract ({status, body:str}) holds.
        try:
            body = json.dumps(data)
        except (TypeError, ValueError):
            body = data.get("_raw", "") if isinstance(data, dict) else ""
        return {"ok": True, "status": 200, "body": body}

    return _with_teams_tab(_run)


def messages(args, ctx):
    conv_id = (args.get("conversation_id") or args.get("id") or "").strip()
    if not conv_id:
        return {"ok": False, "error": "conversation_id required",
                "error_category": "application"}
    try:
        page_size = max(1, min(50, int(args.get("page_size", 20) or 20)))
    except (TypeError, ValueError):
        page_size = 20

    def _run(target):
        from urllib.parse import quote
        url = ("%s/v1/users/ME/conversations/%s/messages?pageSize=%d"
               % (_NG_HOST, quote(conv_id, safe=""), page_size))
        data, err = _fetch_json(target, url)
        if err:
            return err
        import re as _re
        msgs = []
        for m in (data.get("messages") or []):
            content = m.get("content", "") or ""
            clean = _re.sub(r"<[^>]+>", "", content).strip()
            msgs.append({
                "ts": m.get("originalarrivaltime", ""),
                "sender": m.get("imdisplayname", ""),
                "sender_id": m.get("from", ""),
                "messagetype": m.get("messagetype", ""),
                "content": clean,
                "has_html": content != clean,
            })
        return {"ok": True, "count": len(msgs), "conversation_id": conv_id, "messages": msgs}

    return _with_teams_tab(_run)


def status(args, ctx):
    def _run(target):
        # A cheap authed call: fetch 1 conversation; signed_in iff it returns data.
        data, err = _fetch_json(
            target, "%s/v1/users/ME/conversations?pageSize=1" % _NG_HOST)
        if err:
            return {"ok": True, "signed_in": False, "detail": err.get("error", "")}
        return {"ok": True, "signed_in": bool(data.get("conversations") is not None),
                "region": _NG_HOST}
    return _with_teams_tab(_run)


METHODS = {"conversations": conversations, "messages": messages, "status": status,
           "fetch": fetch, "local_conversations": local_conversations}
CAPABILITY = "cap:integrations.teams"
VARIANT = "sso"
ATTRS = {"sso": True, "headed": False}
