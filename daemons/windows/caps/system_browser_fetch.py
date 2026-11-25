"""capability: cap:system.browser.fetch  (Windows, via a NON-HEADLESS Edge + CDP).

The authenticated-page fetcher for sites that a plain `requests` GET can't reach:
sites gated by HTTP Basic AND/OR Microsoft Entra SSO behind an internal gateway
(e.g. the MUM publishing portal `mumportaldocs.seli.wh.rnd.internal.ericsson.com`).

Why a browser (and why NON-HEADLESS) — proven during the MUM investigation:
  * The gateway host chrome-errors under HEADLESS Edge; a NON-HEADLESS Chromium
    reaches it. So this cap forces a non-headless managed Edge (ensure_mode).
  * The site challenges HTTP Basic first. An automated browser can't type into
    the native Basic dialog, so we answer it PROGRAMMATICALLY over CDP:
    Fetch.enable{handleAuthRequests:true} → on Fetch.authRequired reply
    continueWithAuth{response:"ProvideCredentials", username, password}.
  * After Basic it redirects to Entra SSO; the managed profile already carries
    the signed-in session (primary refresh token), so SSO completes silently.

The Basic credentials are supplied PER CALL by the caller (the credential app,
app-to-app) — never stored in the daemon.

Method:
  get(url, username?, password?, settle_s?, timeout_s?, keep_tab?)
      -> {ok, status, final_url, content_type, html} on success
      -> {ok:False, needs_interactive:True, ...} if it sticks on an Entra login
      -> {ok:False, error, error_category} otherwise

Uses a dedicated raw CDP WebSocket (not _cdp_common.ws_send_recv) because
Fetch.authRequired arrives as an ASYNC event interleaved with command replies.
"""
from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlparse

from . import _cdp_common as cdp

try:
    from . import _edge_profile as _edge
except Exception:  # noqa: BLE001
    _edge = None

_LOGIN_MARKERS = ("sign in to your account", "stay signed in",
                  "login.microsoftonline.com", "enter password",
                  "we couldn't sign you in")


class _CDPSession:
    """A raw CDP WebSocket with an ID map + an event queue, so we can both issue
    commands (await their reply by id) and react to async events (authRequired).
    A single background reader thread demultiplexes the socket."""

    def __init__(self, ws_url, timeout=30):
        self.ws = cdp.connect_ws(ws_url, timeout=timeout)
        self._id = 0
        self._lock = threading.Lock()
        self._replies = {}
        self._events = []
        self._ev_lock = threading.Lock()
        self._alive = True
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        while self._alive:
            try:
                raw = self.ws.recv()
            except Exception:  # noqa: BLE001 — socket closed/broken
                self._alive = False
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg:
                self._replies[msg["id"]] = msg
            elif "method" in msg:
                with self._ev_lock:
                    self._events.append(msg)

    def send(self, method, params=None, session_id=None):
        with self._lock:
            self._id += 1
            mid = self._id
        payload = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.ws.send(json.dumps(payload))
        return mid

    def call(self, method, params=None, timeout=20):
        mid = self.send(method, params)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._replies.pop(mid, None)
            if msg is not None:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {}) or {}
            if not self._alive:
                raise RuntimeError(f"CDP socket closed during {method}")
            time.sleep(0.01)
        raise TimeoutError(f"CDP {method} timed out after {timeout}s")

    def send_only(self, method, params=None):
        self.send(method, params)

    def drain_events(self):
        with self._ev_lock:
            evs, self._events = self._events, []
        return evs

    def close(self):
        self._alive = False
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def _ensure_headed_endpoint():
    """Force a NON-HEADLESS managed Edge and point _cdp_common at it. Returns an
    error dict or None. Falls back to whatever is reachable if the managed
    profile helper is unavailable."""
    if _edge is None:
        return cdp.ensure_available()
    dbg = _edge.ensure_mode(headless=False)
    if not dbg:
        return {"ok": False, "error": "could not launch a non-headless managed Edge "
                "(needed to reach the gateway host)", "error_category": "unavailable"}
    cdp.configure(dbg)
    return cdp.ensure_available()


def _looks_like_login(html):
    low = (html or "")[:4000].lower()
    return any(m in low for m in _LOGIN_MARKERS)


def get(args, ctx):
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url required", "error_category": "application"}
    username = args.get("username") or args.get("user") or ""
    password = args.get("password") or ""
    settle = float(args.get("settle_s", 2))
    timeout_s = float(args.get("timeout_s", 60))
    keep_tab = bool(args.get("keep_tab"))

    bad = _ensure_headed_endpoint()
    if bad:
        return bad

    # Open a dedicated tab off to the side and attach a raw CDP session to it.
    opened = None
    sess = None
    try:
        o = cdp.open_tab("about:blank")
        opened = o.get("id")
        target = cdp.wait_for_ws(opened, timeout=10)
        if not target or "webSocketDebuggerUrl" not in target:
            return {"ok": False, "error": "could not open a browser tab",
                    "error_category": "unavailable"}
        sess = _CDPSession(target["webSocketDebuggerUrl"], timeout=timeout_s)
        sess.call("Page.enable", timeout=10)
        sess.call("Runtime.enable", timeout=10)
        # Enable Fetch WITH auth handling so Basic challenges come to us.
        sess.call("Fetch.enable", {"handleAuthRequests": True,
                                   "patterns": [{"urlPattern": "*"}]}, timeout=10)

        provided_auth = {"n": 0}

        def _handle_events():
            """Answer any pending Fetch events: authRequired (Basic) + paused."""
            for ev in sess.drain_events():
                m = ev.get("method")
                p = ev.get("params") or {}
                if m == "Fetch.authRequired":
                    rid = p.get("requestId")
                    challenge = (p.get("authChallenge") or {})
                    source = challenge.get("source")  # "Server" | "Proxy"
                    if source == "Server" and username:
                        provided_auth["n"] += 1
                        sess.send_only("Fetch.continueWithAuth", {
                            "requestId": rid,
                            "authChallengeResponse": {
                                "response": "ProvideCredentials",
                                "username": username, "password": password}})
                    else:
                        # No creds / proxy challenge → let the browser default.
                        sess.send_only("Fetch.continueWithAuth", {
                            "requestId": rid,
                            "authChallengeResponse": {"response": "Default"}})
                elif m == "Fetch.requestPaused":
                    # Not an auth stage → just let it continue unmodified.
                    sess.send_only("Fetch.continueRequest",
                                   {"requestId": p.get("requestId")})

        # Navigate and pump events until the page is loaded (or timeout).
        sess.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout_s
        loaded = False
        while time.time() < deadline:
            _handle_events()
            try:
                rs = sess.call("Runtime.evaluate",
                               {"expression": "document.readyState",
                                "returnByValue": True}, timeout=5)
                if (rs.get("result", {}) or {}).get("value") == "complete":
                    loaded = True
                    # one more event sweep + brief settle for client redirects
                    _handle_events()
                    break
            except Exception:  # noqa: BLE001 — mid-navigation eval can transiently fail
                pass
            time.sleep(0.2)

        if settle > 0:
            end = time.time() + settle
            while time.time() < end:
                _handle_events()
                time.sleep(0.1)

        # Wait for CLIENT-SIDE rendering: this doc (and many like it) loads its
        # real body via JS AFTER readyState==complete. Poll the body size until
        # it STABILISES (two equal reads) or the deadline, so we capture the
        # populated DOM rather than the empty shell.
        if loaded:
            last_len, stable = -1, 0
            while time.time() < deadline and stable < 2:
                _handle_events()
                try:
                    bl = sess.call("Runtime.evaluate",
                                   {"expression": "(document.body?document.body.innerHTML.length:0)",
                                    "returnByValue": True}, timeout=5)
                    cur = int((bl.get("result", {}) or {}).get("value") or 0)
                except Exception:  # noqa: BLE001
                    cur = -1
                if cur > 0 and cur == last_len:
                    stable += 1
                else:
                    stable = 0
                last_len = cur
                time.sleep(0.4)

        # Turn Fetch back off so lingering paused requests don't wedge the tab.
        try:
            sess.call("Fetch.disable", timeout=5)
        except Exception:  # noqa: BLE001
            pass

        # Read final URL + the rendered HTML.
        final_url = url
        try:
            fu = sess.call("Runtime.evaluate",
                           {"expression": "location.href", "returnByValue": True},
                           timeout=5)
            final_url = (fu.get("result", {}) or {}).get("value") or url
        except Exception:  # noqa: BLE001
            pass
        html = ""
        try:
            hv = sess.call("Runtime.evaluate",
                           {"expression": "document.documentElement.outerHTML",
                            "returnByValue": True}, timeout=15)
            html = (hv.get("result", {}) or {}).get("value") or ""
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"could not read page HTML: {exc}",
                    "error_category": "application"}

        host = urlparse(final_url).hostname or urlparse(url).hostname or ""
        # Stuck on an Entra sign-in page → caller needs a one-time interactive
        # login. This is NOT a capability failure (the fetch mechanism worked),
        # so return ok:True with needs_interactive so the structured flag + the
        # final_url survive the transport (a {ok:False} cap result is flattened
        # to an error string by the registry).
        if _looks_like_login(html) or "login.microsoftonline.com" in (final_url or ""):
            return {"ok": True, "needs_interactive": True, "status": 401,
                    "final_url": final_url, "host": host, "content_type": "text/html",
                    "html": "", "auth_provided": provided_auth["n"],
                    "detail": "the page is the Microsoft sign-in — sign in once in the "
                              "managed browser so the session carries next time"}
        if not loaded and not html:
            return {"ok": False, "error": "page did not load before timeout",
                    "error_category": "unavailable", "final_url": final_url}
        return {"ok": True, "status": 200, "final_url": final_url, "host": host,
                "content_type": "text/html", "html": html,
                "auth_provided": provided_auth["n"], "bytes": len(html)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "error_category": "application"}
    finally:
        if sess is not None:
            sess.close()
        if opened and not keep_tab:
            cdp.close_tab(opened)


METHODS = {"get": get}
CAPABILITY = "cap:system.browser.fetch"
VARIANT = "sso"
ATTRS = {"sso": True, "headed": True}
