"""_cdp_common — shared Chromium DevTools Protocol primitives for the browser
capabilities (cap:system.cdp, cap:system.browser.token, cap:system.browser.cookie).

NOT a capability itself (the daemon skips `_`-prefixed modules), just the common
HTTP+WebSocket plumbing against the local Edge debug endpoint so the token/cookie
caps don't each re-implement tab open / eval / getCookies / close. Every browser
capability is endpoint-bound for the same reason: it drives THIS machine's
signed-in Edge, which holds the corporate SSO session.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

DEBUG_URL = os.environ.get("CDP_DEBUG_URL", "http://127.0.0.1:9222").rstrip("/")


def configure(debug_url):
    """Set the Edge DevTools debug endpoint the browser capabilities talk to.

    Precedence is applied by the CALLER (agent.py): env CDP_DEBUG_URL wins, else
    the daemon config's cdp_debug_url, else the built-in default. An explicit env
    value is NOT overridden here (the import-time default already captured it), so
    only a non-empty `debug_url` with no env override changes anything."""
    global DEBUG_URL
    if os.environ.get("CDP_DEBUG_URL"):
        return  # env already won at import; keep it
    if debug_url:
        DEBUG_URL = str(debug_url).rstrip("/")


def http_json(path, method="GET", timeout=5):
    req = urllib.request.Request(DEBUG_URL + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def list_targets():
    return http_json("/json")


def page_tabs():
    return [t for t in list_targets() if t.get("type") == "page"]


def target_by_id(tab_id):
    for t in list_targets():
        if t.get("id") == tab_id:
            return t
    return None


def ensure_available():
    """Return None if a debuggable browser is reachable, else an error dict.

    LAZY RELAUNCH: if the probe fails AND we're driving the MANAGED profile
    (not an externally-attached browser via CDP_DEBUG_URL), try to relaunch the
    managed Edge once and re-point at it. This makes resources.stop_browser /
    resources.free safe — stopping the managed Edge to free RAM no longer leaves
    the browser caps dead until a daemon restart; the next real browser op
    brings it back. Best-effort; a relaunch failure returns the normal error.

    #18: a successful probe is CACHED for a few seconds so a burst of cdp ops
    right after a relaunch doesn't each pay a /json/version round-trip (or a
    relaunch re-check). The cache is only populated on success + keyed by the
    live DEBUG_URL, so a stop (which flips the endpoint dead) still re-probes on
    the next op via the stale-URL miss or the TTL expiry."""
    global _AVAIL_OK_UNTIL, _AVAIL_OK_URL
    now = time.time()
    if _AVAIL_OK_URL == DEBUG_URL and now < _AVAIL_OK_UNTIL:
        return None
    try:
        http_json("/json/version", timeout=3)
        _AVAIL_OK_URL = DEBUG_URL
        _AVAIL_OK_UNTIL = now + _AVAIL_TTL_S
        return None
    except Exception as exc:  # noqa: BLE001
        _AVAIL_OK_UNTIL = 0.0  # probe failed → invalidate any cached OK
        # only auto-relaunch the MANAGED profile (never an external CDP endpoint)
        if not os.environ.get("CDP_DEBUG_URL"):
            try:
                from . import _edge_profile as _edge
                dbg = _edge.ensure_running(headless=True)
                if dbg:
                    configure(dbg)
                    http_json("/json/version", timeout=3)
                    _AVAIL_OK_URL = DEBUG_URL
                    _AVAIL_OK_UNTIL = time.time() + _AVAIL_TTL_S
                    return None
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False,
                "error": f"no debuggable browser at {DEBUG_URL} "
                         f"(start Edge with --remote-debugging-port=9222): {exc}",
                "error_category": "unavailable"}


# #18: short-lived cache of a successful availability probe (see ensure_available).
_AVAIL_TTL_S = 5.0
_AVAIL_OK_UNTIL = 0.0
_AVAIL_OK_URL = None


def invalidate_available():
    """Drop the cached availability result — call after a deliberate stop so the
    next op re-probes (and relaunches) instead of trusting a stale OK."""
    global _AVAIL_OK_UNTIL
    _AVAIL_OK_UNTIL = 0.0


def open_tab(url, timeout=10):
    """Open a new tab at url; return its target dict (has id + wsUrl).

    The whole url is ONE query value to the DevTools /json/new? endpoint, so it
    must be FULLY percent-encoded — including & = %. A permissive safe-set (the
    old `:/?=&%`) left a query-string url's own `&`/`=` literal, so DevTools
    parsed only up to the first `&` and opened a TRUNCATED url (this broke
    navigating to e.g. a Cloudflare Access `?aud=…&edge_token_transfer=…&token=…`
    url). Keep only `:/` safe so the target url is preserved intact."""
    t = http_json("/json/new?" + urllib.request.quote(url, safe=":/"),
                  method="PUT", timeout=timeout)
    return t


def close_tab(tab_id, timeout=5):
    if not tab_id:
        return
    try:
        http_json(f"/json/close/{tab_id}", timeout=timeout)
    except Exception:  # noqa: BLE001
        pass


def connect_ws(ws_url, timeout=15):
    import websocket  # websocket-client (sync)
    # max_size=None: CDP responses can be large (e.g. reading a big IndexedDB
    # store returns multi-MB JSON). The library's default frame cap silently
    # DROPS oversized messages, so a big Runtime.evaluate result comes back empty
    # — lift the cap so large reads (the Teams conversation list) survive.
    return websocket.create_connection(ws_url, suppress_origin=True, timeout=timeout,
                                       max_size=None)


# --- short-lived WS pool ----------------------------------------------------
# connect→handshake dominates a small eval/getCookies op. Pool one open WS per
# webSocketDebuggerUrl and reuse it; evict after idle. A wedged socket is dropped
# and reopened on the next use (ws_send_recv already bounds recv by timeout).
_POOL = {}            # ws_url -> {"ws": conn, "last": ts, "lock": Lock}
_POOL_LOCK = threading.Lock()
_POOL_IDLE_S = 30


def _pool_get(ws_url, timeout=15):
    now = time.time()
    with _POOL_LOCK:
        ent = _POOL.get(ws_url)
        if ent is not None and (now - ent["last"]) <= _POOL_IDLE_S:
            ent["last"] = now
            return ent
        # evict stale/closed
        if ent is not None:
            try:
                ent["ws"].close()
            except Exception:  # noqa: BLE001
                pass
        ws = connect_ws(ws_url, timeout=timeout)
        ent = {"ws": ws, "last": now, "lock": threading.Lock()}
        _POOL[ws_url] = ent
        return ent


def _pool_drop(ws_url):
    with _POOL_LOCK:
        ent = _POOL.pop(ws_url, None)
    if ent is not None:
        try:
            ent["ws"].close()
        except Exception:  # noqa: BLE001
            pass


def eval_on_target_pooled(target, expr, timeout=15):
    """Runtime.evaluate reusing a pooled WS for the target; falls back to a fresh
    connection (and drops the pooled one) if the reused socket is wedged."""
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        return {"ok": False, "error": "target has no debugger url",
                "error_category": "unavailable"}
    ent = _pool_get(ws_url, timeout=timeout)
    try:
        with ent["lock"]:
            res = ws_send_recv(ent["ws"], "Runtime.evaluate",
                               {"expression": expr, "returnByValue": True,
                                "awaitPromise": True}, timeout=timeout)
        if "exceptionDetails" in res:
            return {"ok": False,
                    "error": str(res["exceptionDetails"].get("text", "JS error")),
                    "error_category": "application"}
        return {"ok": True, "value": res.get("result", {}).get("value")}
    except Exception:  # noqa: BLE001 — wedged/closed socket: drop + retry once fresh
        _pool_drop(ws_url)
        return eval_on_target(target, expr, timeout=timeout)


_MSG_ID = 0
_MSG_ID_LOCK = threading.Lock()


def ws_send_recv(ws, method, params=None, timeout=15):
    """Send one CDP command on an open ws and return the matching result dict.
    Uses a process-global monotonic id so a pooled/reused socket can't match a
    stale reply from a previous (timed-out) call."""
    global _MSG_ID
    with _MSG_ID_LOCK:
        _MSG_ID += 1
        mid = _MSG_ID
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = json.loads(ws.recv())
        if resp.get("id") == mid:
            return resp.get("result", {}) or {}
    raise TimeoutError(f"CDP {method} timed out after {timeout}s")


def eval_on_target(target, expr, timeout=15):
    """Runtime.evaluate on a target dict; return {ok, value} or {ok:False,...}."""
    ws = connect_ws(target["webSocketDebuggerUrl"], timeout=timeout)
    try:
        res = ws_send_recv(ws, "Runtime.evaluate",
                           {"expression": expr, "returnByValue": True,
                            "awaitPromise": True}, timeout=timeout)
        if "exceptionDetails" in res:
            return {"ok": False,
                    "error": str(res["exceptionDetails"].get("text", "JS error")),
                    "error_category": "application"}
        return {"ok": True, "value": res.get("result", {}).get("value")}
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def wait_for_ws(tab_id, timeout=8):
    """Poll /json until the tab has a webSocketDebuggerUrl (freshly-opened tabs
    may not report one immediately). Returns the target dict or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = target_by_id(tab_id)
        if t and t.get("webSocketDebuggerUrl"):
            return t
        time.sleep(0.25)
    return None


def get_cookies_for(target, urls, timeout=15):
    """Network.getCookies for the given URL(s) on `target`, returning
    {name: value}. Reads HttpOnly cookies too (unlike document.cookie) — needed
    for session cookies like DNN's .DOTNETNUKE (ETSI). Values only, no metadata."""
    ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else None
    if not ws_url:
        return {"ok": False, "error": "target has no debugger url",
                "error_category": "unavailable"}
    try:
        ws = connect_ws(ws_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}
    try:
        res = ws_send_recv(ws, "Network.getCookies",
                           {"urls": list(urls) if urls else []}, timeout=timeout)
        cookies = {c.get("name"): c.get("value") for c in (res.get("cookies") or [])
                   if c.get("name")}
        return {"ok": True, "cookies": cookies, "count": len(cookies)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def capture_network(target, *, url_substr="", header="authorization",
                    duration_s=20, navigate_url="", timeout=25):
    """Capture a request HEADER from the target's network traffic over a window.

    Enables the CDP Network domain on `target`, optionally navigates it, then
    collects `Network.requestWillBeSent` events for up to `duration_s`, returning
    the FIRST request whose URL contains `url_substr` (case-insensitive, empty =
    any) and that carries the named `header` (default 'authorization' — the
    Bearer the flow/Power-Automate plugins need). Unlike ws_send_recv this reads
    the unsolicited event stream (so it can't reuse the pooled eval socket).

    Returns {ok, value, url, method} where value is the header value, or
    {ok:False, error} / {ok:True, value:None, timed_out:True} if nothing matched.
    NEVER returns the whole header set unfiltered (avoid leaking other tokens).
    """
    ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else None
    if not ws_url:
        return {"ok": False, "error": "target has no debugger url",
                "error_category": "unavailable"}
    want_url = (url_substr or "").lower()
    want_hdr = (header or "authorization").lower()
    ws = connect_ws(ws_url, timeout=timeout)
    try:
        ws_send_recv(ws, "Network.enable", {}, timeout=timeout)
        if navigate_url:
            try:
                ws_send_recv(ws, "Page.enable", {}, timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
            ws_send_recv(ws, "Page.navigate", {"url": navigate_url}, timeout=timeout)
        ws.settimeout(1.0)
        deadline = time.time() + max(1, int(duration_s))
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except Exception:  # noqa: BLE001 — recv timeout / transient
                continue
            if msg.get("method") != "Network.requestWillBeSent":
                continue
            req = (msg.get("params") or {}).get("request") or {}
            req_url = (req.get("url") or "")
            if want_url and want_url not in req_url.lower():
                continue
            headers = req.get("headers") or {}
            for k, v in headers.items():
                if k.lower() == want_hdr and v:
                    return {"ok": True, "value": v, "url": req_url,
                            "method": req.get("method", "")}
        return {"ok": True, "value": None, "timed_out": True,
                "detail": "no matching request within %ss" % duration_s}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    finally:
        try:
            ws_send_recv(ws, "Network.disable", {}, timeout=3)
        except Exception:  # noqa: BLE001
            pass
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
