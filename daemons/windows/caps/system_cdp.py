"""capability: cap:system.cdp  (Windows, via a local Chromium/Edge DevTools endpoint).

Endpoint-bound (not win32, but tied to THIS machine's browser): it talks to a
Chromium DevTools Protocol HTTP+WebSocket endpoint on a debug port. On Windows
this is the user's signed-in Edge (variant 'sso' — the browser already holds the
Microsoft/corporate session), which is exactly why CDP is a capability of the
endpoint and not a portable app.

Identity: single-user today — ctx.user maps to THE one local browser. The
`user -> browser profile` seam is kept explicit (the daemon owns which profile a
user's CDP calls hit) so multi-user (per-user Edge user-data-dir) needs no
protocol change later.

Methods:
  tabs()                         -> {ok, tabs:[{id,title,url,type}]}
  navigate(url, tab_id?)         -> {ok, tab_id, title}
  eval(expr, tab_id?)            -> {ok, value}   (Runtime.evaluate, returnByValue)
  status()                       -> {ok, available, debug_url, managed, browser, tabs}

Connection: discover the debug endpoint from CDP_DEBUG_URL env (default
http://127.0.0.1:9222). If no browser is listening there, returns a clear
'unavailable' so the framework surfaces it (the app shows 'needs a debuggable
Edge'). The low-level HTTP+WS plumbing lives in _cdp_common (shared with the
browser.token / browser.cookie capabilities).
"""
from __future__ import annotations

from . import _cdp_common as cdp


def status(args, ctx):
    """Report the browser the capability is driving: the debug endpoint, whether
    it's the ai-pylon2 MANAGED dedicated profile (vs an externally-run browser),
    the browser version string, and the current tab count."""
    debug_url = cdp.DEBUG_URL
    managed = False
    profile_dir = ""
    try:
        from . import _edge_profile as _edge
        profile_dir = _edge.PROFILE_DIR
        # Managed when the active endpoint matches the profile's recorded port.
        import os
        if os.path.isfile(_edge.PORT_FILE):
            p = open(_edge.PORT_FILE).read().strip()
            managed = bool(p) and debug_url.rstrip("/").endswith(":" + p)
    except Exception:  # noqa: BLE001
        pass
    bad = cdp.ensure_available()
    if bad:
        return {"ok": True, "available": False, "debug_url": debug_url,
                "managed": managed, "profile_dir": profile_dir,
                "detail": bad.get("error", "no debuggable browser")}
    browser = ""
    try:
        browser = (cdp.http_json("/json/version", timeout=3) or {}).get("Browser", "")
    except Exception:  # noqa: BLE001
        pass
    try:
        ntabs = len(cdp.page_tabs())
    except Exception:  # noqa: BLE001
        ntabs = 0
    return {"ok": True, "available": True, "debug_url": debug_url,
            "managed": managed, "profile_dir": profile_dir,
            "browser": browser, "tabs": ntabs}


def tabs(args, ctx):
    bad = cdp.ensure_available()
    if bad:
        return bad
    try:
        ts = cdp.page_tabs()
        return {"ok": True, "tabs": [
            {"id": t.get("id"), "title": t.get("title", ""),
             "url": t.get("url", ""), "type": t.get("type", "")}
            for t in ts]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


def navigate(args, ctx):
    bad = cdp.ensure_available()
    if bad:
        return bad
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url required", "error_category": "application"}
    tab_id = args.get("tab_id")
    try:
        if not tab_id:
            t = cdp.open_tab(url)
            if args.get("capture"):
                _tgt = cdp.target_by_id(t.get("id"))
                if _tgt:
                    _record_probe(_tgt, args.get("label") or ("navigate: " + url[:60]),
                                  {"type": "navigate", "url": url})
            return {"ok": True, "tab_id": t.get("id"), "title": t.get("title", ""),
                    "url": t.get("url", url)}
        t = cdp.target_by_id(tab_id)
        if not t or "webSocketDebuggerUrl" not in t:
            return {"ok": False, "error": f"tab '{tab_id}' not found",
                    "error_category": "application"}
        ws = cdp.connect_ws(t["webSocketDebuggerUrl"])
        try:
            cdp.ws_send_recv(ws, "Page.navigate", {"url": url})
        finally:
            ws.close()
        if args.get("capture"):
            _record_probe(t, args.get("label") or ("navigate: " + url[:60]),
                          {"type": "navigate", "url": url})
        return {"ok": True, "tab_id": tab_id, "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


def close(args, ctx):
    """Close only the explicitly named tab; never default to the user's active tab."""
    tab_id = args.get("tab_id")
    if not tab_id:
        return {"ok": False, "error": "tab_id required"}
    try:
        cdp.close_tab(str(tab_id))
        return {"ok": True, "tab_id": tab_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def snapshot(args, ctx):
    """Capture one explicitly named tab, optionally sizing and clipping its viewport."""
    tab_id = args.get("tab_id")
    if not tab_id:
        return {"ok": False, "error": "tab_id required"}
    target = cdp.target_by_id(str(tab_id))
    if not target:
        return {"ok": False, "error": "Unknown tab"}
    ws = cdp.connect_ws(target["webSocketDebuggerUrl"], timeout=15)
    try:
        width, height = int(args.get("width", 1920)), int(args.get("height", 1080))
        if not 320 <= width <= 3840 or not 240 <= height <= 2160:
            return {"ok": False, "error": "Viewport exceeds supported dimensions"}
        if args.get("resize"):
            cdp.ws_send_recv(ws, "Emulation.setDeviceMetricsOverride", {"width":width,"height":height,"deviceScaleFactor":1,"mobile":False})
        params = {"format":"png","captureBeyondViewport":False}
        clip = args.get("clip")
        if clip:
            x,y,w,h = (float(clip[k]) for k in ("x","y","width","height"))
            if min(x,y)<0 or min(w,h)<=0 or x+w>3840 or y+h>2160:
                return {"ok":False,"error":"Invalid capture clip"}
            params["clip"]={"x":x,"y":y,"width":w,"height":h,"scale":1}
        value=cdp.ws_send_recv(ws,"Page.captureScreenshot",params,timeout=15)
        return {"ok":True,"image_b64":value["data"],"format":"png","tab_id":tab_id,"source_url":target.get("url","")}
    except Exception as exc:
        return {"ok":False,"error":str(exc)}
    finally:
        ws.close()


def _capture_shot_once(tgt):
    """Grab ONE base64-jpeg screenshot of a target via a short-lived WS. Returns
    '' on any failure. Used by the manual-probe capture (#6)."""
    try:
        ws = cdp.connect_ws(tgt["webSocketDebuggerUrl"], timeout=10)
        try:
            r = cdp.ws_send_recv(ws, "Page.captureScreenshot",
                                 {"format": "jpeg", "quality": 40}, timeout=12)
            return (r or {}).get("data", "") if isinstance(r, dict) else ""
        finally:
            ws.close()
    except Exception:  # noqa: BLE001
        return ""


def _record_probe(tgt, label, action):
    """#6: capture a screenshot of `tgt` and POST it + the probe action to
    page_automation.probe, so a manual eval/navigate/click during debugging is
    recorded VISUALLY in the same run filmstrip. Best-effort — never raises, and
    a capture/post failure must not fail the underlying eval/navigate."""
    try:
        shot = _capture_shot_once(tgt)
        _hub_post_app("platform/page_automation", "probe",
                      {"label": label, "action": action, "shot_b64": shot,
                       "confirm": True})
    except Exception:  # noqa: BLE001
        pass


def eval_js(args, ctx):
    bad = cdp.ensure_available()
    if bad:
        return bad
    expr = args.get("expr") or args.get("expression") or ""
    if not expr:
        return {"ok": False, "error": "expr required", "error_category": "application"}
    tab_id = args.get("tab_id")
    try:
        tgt = cdp.target_by_id(tab_id) if tab_id else (cdp.page_tabs() or [None])[0]
        if not tgt or "webSocketDebuggerUrl" not in tgt:
            return {"ok": False, "error": "no debuggable page tab available",
                    "error_category": "unavailable"}
        res = cdp.eval_on_target_pooled(tgt, expr)
        # #6: opt-in — record this manual eval + a screenshot into the probe trail.
        if args.get("capture"):
            _record_probe(tgt, args.get("label") or ("eval: " + expr[:60]),
                          {"type": "eval", "expr": expr[:300], "url": tgt.get("url", "")})
        return res
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


def signin(args, ctx):
    """Open the MANAGED Edge profile NON-HEADLESS on the account sign-in page so
    the user can sign in to their Microsoft/Entra account (and turn on Sync)
    ONCE. After this one interactive sign-in the profile persists the live Entra
    session (incl. the login.microsoftonline.com cookies), so subsequent SSO —
    e.g. the MUM authorize redirect, token/cookie extraction — completes
    silently, exactly like the old ai-pylon persistent profile.

    This is the 'create a CDP profile synced with my account' action: it makes
    the managed profile a REAL signed-in profile rather than a minimal seed.
    Optional `url` overrides where to land (default: the Edge profile sign-in).
    """
    try:
        from . import _edge_profile as _edge
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"managed profile unavailable: {exc}",
                "error_category": "unavailable"}
    # Force the managed Edge to run NON-HEADLESS (an interactive sign-in / the
    # WAM broker cannot complete under headless).
    dbg = _edge.ensure_mode(headless=False)
    if not dbg:
        return {"ok": False, "error": "could not launch the managed Edge non-headless",
                "error_category": "unavailable"}
    cdp.configure(dbg)
    # Land on Edge's OWN profile/account settings so the user signs in to the
    # PROFILE (which enables Sync and persists the Entra session), not merely a
    # website. Signing into a web page alone does not sync the profile.
    url = (args.get("url") or "edge://settings/profiles").strip()
    try:
        t = cdp.open_tab(url)
        return {"ok": True, "debug_url": dbg, "profile_dir": _edge.PROFILE_DIR,
                "tab_id": t.get("id"), "url": t.get("url", url), "headless": False,
                "detail": "Sign in with your account in the Edge window that opened "
                          "(and enable Sync). This is a ONE-TIME step; the session "
                          "then persists for silent SSO."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


def _surface_edge_window():
    """Flip the managed Edge NON-HEADLESS so the user can see + complete an
    interactive human gate (MFA / captcha / password). Best-effort; re-points CDP at the (possibly
    relaunched) endpoint. No-op if the managed profile helper is unavailable."""
    try:
        from . import _edge_profile as _edge
        dbg = _edge.ensure_mode(headless=False)
        if dbg:
            cdp.configure(dbg)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


_MFA_NOTIFY_ID = "mfa_signin"


def _fw_url():
    """The framework base URL this daemon dials (for posting to the notify hub).
    The agent sets PYLON2_FW_URL; default to the local control plane."""
    import os
    return (os.environ.get("PYLON2_FW_URL") or "http://127.0.0.1:8900").rstrip("/")


def _hub_post(op, body):
    """Best-effort POST to a framework app op (used to reach the notify hub from
    the daemon, which can't appcall in-process). Never raises."""
    return _hub_post_app("system/notify", op, body)


def _hub_post_app(app_id, op, body):
    """Best-effort POST to ANY framework app op from the daemon (which can't
    appcall in-process). Returns True on a 2xx, False otherwise. Never raises."""
    try:
        import json as _j
        import urllib.request as _u
        req = _u.Request(_fw_url() + "/v1/apps/" + app_id.strip("/") + "/" + op,
                         data=_j.dumps(body).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, method="POST")
        _u.urlopen(req, timeout=8).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def _notify_mfa(uri, code, ctx, number=""):
    """Surface the approval as a PERSISTENT, self-updating hub notification
    (stable id so repeated number updates refresh ONE record rather than
    spamming) — it stays visible until the sign-in is resolved/denied/expired
    (see _resolve_mfa). Also fires the tray balloon for immediacy. The wording is
    provider-aware: a Microsoft/Entra URL says "Microsoft Authenticator"; any
    other provider (e.g. Google) gets NEUTRAL wording so a non-Entra login never
    shows a bogus 'Microsoft sign-in' prompt."""
    _u = (uri or "").lower()
    _is_ms = any(h in _u for h in ("login.microsoftonline.com", "login.microsoft.com",
                                   "login.live.com", "microsoft.com/devicelogin", "msauth"))
    _app = "Microsoft Authenticator" if _is_ms else "your authenticator app"
    if number:
        title = "Approve sign-in: enter %s" % number
        msg = ("Open %s and enter the number %s to approve the sign-in. "
               "This stays until you approve or it expires." % (_app, number))
    else:
        title = "Finish Microsoft sign-in" if _is_ms else "Finish signing in"
        msg = ("Approve the request in %s on your phone." % _app) if _is_ms \
            else "Finish the sign-in in your browser / approve it on your phone."
    # Persistent hub notification (upsert by stable id → one record, refreshed).
    _hub_post("upsert", {"id": _MFA_NOTIFY_ID, "title": title, "message": msg,
                         "level": "pending", "source": "device_login", "kind": "pending",
                         "status": "active", "url": uri,
                         "payload": {"number": number, "code": code}})
    # Immediate tray balloon too (best-effort).
    try:
        from . import system_tray as _tray
        _tray.notify({"title": title, "message": msg, "level": "pending"}, ctx or {})
    except Exception:  # noqa: BLE001
        pass


def _resolve_mfa(status):
    """Mark the persistent MFA notification done/failed (resolved|denied|expired)
    when the sign-in reaches a terminal state — so it stops showing as pending."""
    _hub_post("resolve", {"id": _MFA_NOTIFY_ID, "status": status})


# The automation-language schema version this interpreter IMPLEMENTS. Must match
# apps/platform/page_automation store.SCHEMA_VERSION; a mismatch is surfaced by
# the orchestrator so the validator + interpreter can't silently drift.
_INTERP_SCHEMA_VERSION = 5


def _seed_bundle():
    """The built-in entra_device_login case (FLATTENED — the daemon doesn't
    resolve fragments). Used when a caller passes NO `case` bundle (back-compat
    with the old user_code/password/account args). Kept in lock-step with
    apps/platform/page_automation store.py SEED_CASES + SEED_FRAGMENTS (the
    resolved form of entra_device_login)."""
    return {
        "id": "entra_device_login",
        "match": {"url_contains": ["login.microsoftonline.com", "microsoft.com/devicelogin",
                                   "login.microsoft.com/device", "/deviceauth"]},
        "defaults": {"normalize_uri": "https://microsoft.com/devicelogin",
                     "pre_gate_budget_s": 60,
                     "wait_for_selector": "input#otc,input[name=otc],input[type=tel],input[type=text]"},
        "screens": [
            {"name": "transient_900561", "when": {"text_any": ["aadsts900561"]},
             "classify": "wait", "detail": "transient POST/GET on redirect — waiting"},
            {"name": "denied", "when": {"text_any": ["aadsts90135", "decided not to continue",
                                                     "you don't have access", "need admin approval",
                                                     "we couldn't sign you in",
                                                     "your account or password is incorrect"]},
             "classify": "denied", "detail": "tenant/app denied or credential rejected"},
            {"name": "completed", "when": {"text_any": ["you may now close", "you have signed in",
                                                        "sign in was successful",
                                                        "authentication complete", "you're all set"]},
             "classify": "completed", "detail": "sign-in completed"},
            {"name": "mfa", "when": {"text_any": ["approve sign in request",
                                                  "we've sent a notification",
                                                  "open your authenticator", "enter the number shown",
                                                  "enter the number to sign in",
                                                  "approve the request", "verify your identity",
                                                  "number to sign in", "to sign in, enter",
                                                  "sent a request to your", "approve the sign-in",
                                                  "check your microsoft authenticator",
                                                  "waiting for you to approve", "didn't receive a request"]},
             "classify": "mfa", "detail": "authenticator approval required — enter the number shown, then approve on your phone",
             "do": [{"type": "scrape_number"}, {"type": "notify"}]},
            {"name": "code_entry",
             "when": {"selector": "input#otc,input[name=otc]"},
             "classify": "continue",
             "do": [{"type": "fill", "selector": "input#otc,input[name=otc],input[type=tel],input[type=text]",
                     "value": "${user_code}"},
                    {"type": "click_primary"}, {"type": "wait", "seconds": 4}]},
            {"name": "password", "when": {"selector": "#i0118,#passwordInput,input[type=password]",
                                          "text_any": ["enter password", "enter your password"]},
             "classify": "password",
             "do": [{"type": "type_secret", "selector": "#i0118,#passwordInput,input[type=password]",
                     "ref": "master"},
                    {"type": "click_primary"}, {"type": "wait", "seconds": 4}]},
            {"name": "account_tile", "when": {"text_any": ["pick an account"],
                                              "not_selector": "#i0116,input[type=email]"},
             "classify": "continue",
             "do": [{"type": "click_tile", "match": "${account}", "other_tile": True},
                    {"type": "wait", "seconds": 4}]},
            {"name": "kmsi", "when": {"selector": "#KmsiDescription,#kmsiDescription",
                                      "text_any": ["stay signed in"]},
             "classify": "continue", "do": [{"type": "click_primary"}, {"type": "wait", "seconds": 2}]},
        ],
    }


def device_login(args, ctx):
    """Generic CASE INTERPRETER for a browser login/consent flow driven over the
    managed (signed-in) Edge. The RULES — screen classification, selectors, and
    the action sequence per screen — are supplied as a `case` BUNDLE by the
    platform/page_automation app (one editable source of truth), NOT hardcoded
    here. This cap only INTERPRETS the bundle and does the CDP driving (trusted
    Input.* mouse/keyboard, tab hygiene, the human-gate wait).

    Payload (new, from page_automation.run):
      case     — {id, match, defaults, screens[]} bundle (see page_automation)
      inputs   — {user_code, account, verification_uri, ...} substituted into
                 ${...} action placeholders
      secrets  — {ref: plaintext} resolved IN-PROCESS by the orchestrator (the
                 daemon never touches the vault) — a type_secret action names a ref
      human_timeout_s — how long to wait at a human gate (default 180)

    Back-compat (old callers: auth_http/llm_server): if NO `case` is given, the
    built-in _seed_bundle() is used and the legacy args user_code/password/
    account/verification_uri/timeout_s/wait_for_human are mapped into inputs +
    secrets['master'].

    Returns {ok, state: completed|denied|needs_human|pending, detail,
    mfa_number, human_pending, final_text}."""
    import time as _t
    import json as _json
    bad = cdp.ensure_available()
    if bad:
        return bad

    # --- resolve case bundle + inputs + secrets (new payload or legacy args) --
    case = args.get("case")
    if not isinstance(case, dict) or not case.get("screens"):
        case = _seed_bundle()
    defaults = case.get("defaults", {}) or {}
    inputs = dict(args.get("inputs") or {})
    secrets = dict(args.get("secrets") or {})
    # legacy arg mapping
    if args.get("user_code") or args.get("code"):
        inputs.setdefault("user_code", args.get("user_code") or args.get("code"))
    if args.get("account") is not None:
        inputs.setdefault("account", args.get("account"))
    if args.get("verification_uri"):
        inputs.setdefault("verification_uri", args.get("verification_uri"))
    if args.get("password") is not None and "master" not in secrets:
        secrets["master"] = args.get("password")

    is_gather = bool(case.get("gather"))
    code = (inputs.get("user_code") or "").strip()
    if is_gather:
        # DATA-GATHER case: no device code; open the start URL (inputs.url or the
        # case's defaults.start_url) and drive navigate/click/extract screens.
        uri = (inputs.get("url") or defaults.get("start_url") or "").strip()
        if not uri:
            return {"ok": False, "error": "gather case needs inputs.url or defaults.start_url",
                    "error_category": "application"}
    else:
        if not code:
            # No device code: a WEB-FORM login case (e.g. google_web_login) that
            # DRIVES ITS OWN start URL instead of a device-code page. Open
            # inputs.url / defaults.start_url and run the email/password/MFA
            # screens. Only error if the case gave no URL to open either.
            uri = (inputs.get("url") or defaults.get("start_url") or "").strip()
            if not uri:
                return {"ok": False, "error": "inputs.user_code required "
                        "(or defaults.start_url for a web-form login)",
                        "error_category": "application"}
        else:
            uri = (inputs.get("verification_uri") or "https://microsoft.com/devicelogin").strip()
            # Normalize the device URL (the MSAL shortlink 302s to a POST-only
            # endpoint; opened over CDP as a GET it yields AADSTS900561).
            _norm = defaults.get("normalize_uri") or "https://microsoft.com/devicelogin"
            if "login.microsoft.com/device" in uri or uri.rstrip("/").endswith("/device"):
                uri = _norm
    budget = float(defaults.get("pre_gate_budget_s") or args.get("timeout_s") or 60)
    wait_human = args.get("wait_for_human", True)
    human_budget = float(args.get("human_timeout_s") or 180)
    collected = {}   # data-gather bucket (extract actions fill this)

    def _subst(val):
        """Substitute ${user_code}/${account}/... placeholders from inputs."""
        if not isinstance(val, str):
            return val
        out = val
        for k, v in inputs.items():
            out = out.replace("${%s}" % k, str(v))
        return out

    # Put the managed Edge in NON-HEADLESS mode ONCE, BEFORE opening the device
    # tab — so we never have to flip modes mid-flow (a mid-flow ensure_mode kills
    # + relaunches Edge, destroying the device tab and invalidating the device
    # authorization → the AADSTS900561 GET/POST cascade). Set it up front, then
    # never touch the mode again during this flow.
    try:
        from . import _edge_profile as _edge
        _dbg = _edge.ensure_mode(headless=False)
        if _dbg:
            cdp.configure(_dbg)
    except Exception:  # noqa: BLE001
        pass

    tab = None
    _iws = {"ws": None}   # dedicated WS for trusted Input.* events (closed in finally)
    _shot_ws = {"ws": None}   # dedicated WS reused for all screenshots (perf #21)
    human_pending = [False] # set True at an Authenticator gate → keep the tab OPEN
    last_notify = [0.0]
    try:
        # TAB HYGIENE (AUTH_DEVICE_LOGIN #5): close stale/orphan Entra login tabs
        # from a prior run BEFORE opening a fresh one. Orphans (a second
        # /deviceauth, a left-open /reprocess) caused tab-identity races where the
        # driver typed into one tab while reads hit another. Close any page tab
        # whose URL is a known login/device/approval endpoint; never touch other
        # tabs (user content). Best-effort — a close failure just leaves it.
        _ORPHAN = ("login.microsoftonline.com", "microsoft.com/devicelogin",
                   "login.microsoft.com/device", "/deviceauth", "/appverify",
                   "/reprocess", "/sas/processauth", "/common/oauth2/deviceauth")
        _attach = (args.get("attach_tab_id") or "").strip()
        try:
            if not _attach:   # don't prune tabs when we're attaching to one
                for _ot in (cdp.page_tabs() or []):
                    _ourl = (_ot.get("url") or "").lower()
                    if _ourl and any(k in _ourl for k in _ORPHAN):
                        cdp.close_tab(_ot.get("id"))
        except Exception:  # noqa: BLE001
            pass
        # ATTACH to an existing login tab (continue the ladder where the user
        # left off after solving a challenge) instead of opening a fresh one.
        tab = None
        if _attach:
            _at = cdp.target_by_id(_attach)
            if _at and _at.get("webSocketDebuggerUrl"):
                tab = {"id": _attach}
        if tab is None:
            tab = cdp.open_tab(uri)
        tab_id = tab.get("id")
        cdp.wait_for_ws(tab_id, timeout=10)
        tgt = cdp.target_by_id(tab_id)
        if not tgt:
            return {"ok": False, "error": "could not open device-login tab",
                    "error_category": "unavailable"}
        deadline = _t.time() + budget

        def ev(expr):
            r = cdp.eval_on_target_pooled(tgt, expr)
            return r.get("value") if isinstance(r, dict) else None

        # A dedicated WS for trusted Input.* events (real mouse/keyboard), which
        # Entra honors where a scripted .value/.click() is rejected. Edge's
        # autocomplete overlay also no longer blocks us: we FOCUS the field with a
        # real click, select-all + delete, then TYPE the password char-by-char.
        def _cdp(method, params=None):
            if _iws["ws"] is None:
                _iws["ws"] = cdp.connect_ws(tgt["webSocketDebuggerUrl"], timeout=15)
            return cdp.ws_send_recv(_iws["ws"], method, params or {}, timeout=15)

        def _box(selector_js):
            b = ev("(function(){var e=%s;if(!e)return '';e.scrollIntoView({block:'center'});"
                   "var r=e.getBoundingClientRect();return JSON.stringify({x:r.left+r.width/2,"
                   "y:r.top+r.height/2});})()" % selector_js)
            if not b:
                return None
            try:
                return _json.loads(b)
            except Exception:  # noqa: BLE001
                return None

        def real_click(selector_js):
            p = _box(selector_js)
            if not p:
                return False
            for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
                prm = {"type": typ, "x": p["x"], "y": p["y"]}
                if typ != "mouseMoved":
                    prm.update(button="left", clickCount=1)
                _cdp("Input.dispatchMouseEvent", prm)
            return True

        def type_secret(selector, pw):
            """Focus the field, CLEAR it, then type the secret CHARACTER-BY-
            CHARACTER via trusted keystrokes (Input.dispatchKeyEvent). Google's
            password field is a React-controlled input: a single Input.insertText
            (or a scripted .value) updates the VISIBLE value but NOT React's
            internal state, so the submit sends an EMPTY/stale password and Google
            rejects it (the 'password not filling / loops back to pwd' bug). Real
            per-char keyDown/keyUp fire the keypress/input events React listens
            to, so its state tracks the value. insertText is kept as a fallback."""
            sel_js = "document.querySelector(%r)" % selector
            # Focus: trusted click + JS .focus() fallback (field box can be
            # 0/off-screen mid-transition, so the click alone can miss).
            real_click(sel_js)
            try:
                ev("(function(){var e=%s;if(e){e.focus();}})()" % sel_js)
            except Exception:  # noqa: BLE001
                pass
            _t.sleep(0.2)
            # CLEAR any existing value (leftover autofill / a stray char the user
            # or a prior step typed): select-all then Delete via trusted keys.
            _cdp("Input.dispatchKeyEvent", {"type": "keyDown", "modifiers": 2,
                 "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65})
            _cdp("Input.dispatchKeyEvent", {"type": "keyUp", "modifiers": 2,
                 "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65})
            _cdp("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Delete",
                 "code": "Delete", "windowsVirtualKeyCode": 46})
            _cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Delete",
                 "code": "Delete", "windowsVirtualKeyCode": 46})
            _t.sleep(0.1)
            # TYPE each char as a real keystroke so React's onChange fires and its
            # internal state tracks the value (the critical fix). `char` events
            # (keyDown with text + keyUp) are the trusted-input equivalent of a
            # user typing.
            for ch in str(pw):
                _cdp("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "key": ch})
                _cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
                _t.sleep(0.02)
            _t.sleep(0.2)
            # verify the field now holds EXACTLY our secret length.
            lead = ("(function(){var e=%s;" % sel_js)
            got = ev(lead + "return e?(e.value||'').length:-1;})()")
            if got != len(str(pw)):
                # fallback 1: insertText (covers an input that ignores char events)
                ev(lead + "if(e){var s=Object.getOwnPropertyDescriptor("
                   "window.HTMLInputElement.prototype,'value').set;s.call(e,'');"
                   "e.dispatchEvent(new Event('input',{bubbles:true}));}})()")
                _cdp("Input.insertText", {"text": str(pw)})
                _t.sleep(0.2)
                got = ev(lead + "return e?(e.value||'').length:-1;})()")
            return got == len(str(pw))

        def fill_value(selector, value):
            """Native value-set + input/change events for a non-sensitive field
            (e.g. the device code). A real click-then-submit follows separately."""
            return ev("(function(){var e=document.querySelector(%r);if(!e)return 0;"
                      "var set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"
                      "'value').set;set.call(e,%s);"
                      "e.dispatchEvent(new Event('input',{bubbles:true}));"
                      "e.dispatchEvent(new Event('change',{bubbles:true}));return 1;})()"
                      % (selector, _json.dumps(str(value))))

        # Every click helper returns the EXACT CSS selector it resolved + acted
        # on (or "" if nothing). Callers record that selector in the trace so the
        # step is REPLAYABLE against the concrete object — not a fuzzy re-scan.
        def click_tile(match_sub, other_tile=False):
            """Pick an account tile by data-test-id email substring (trusted click).
            Returns the EXACT selector clicked. If none matches and other_tile is
            set, click a 'use another account' control instead."""
            want = _json.dumps((match_sub or "").strip().lower())
            tid = ev("(function(){var tiles=[].slice.call(document.querySelectorAll('[data-test-id]'));"
                     "var want=%s;for(var i=0;i<tiles.length;i++){var id=(tiles[i].getAttribute('data-test-id')||'').toLowerCase();"
                     "if(id.indexOf('@')>=0&&(!want||id.indexOf(want)>=0)){return tiles[i].getAttribute('data-test-id');}}"
                     "return '';})()" % want)
            if tid:
                sel = '[data-test-id="%s"]' % tid
                return sel if real_click("document.querySelector(%r)" % sel) else ""
            if other_tile:
                # fall back to a 'use another account' control (id or text).
                sel = ev("(function(){var o=document.querySelector('#otherTile,#otherTileText');"
                         "if(o)return '#'+(o.id||'otherTile');"
                         "var c=[].slice.call(document.querySelectorAll('[role=button],.tile-container,div.table'));"
                         "for(var i=0;i<c.length;i++){var t=(c[i].textContent||'').toLowerCase();"
                         "if(t.indexOf('another account')>=0||t.indexOf('use another')>=0){"
                         "c[i].setAttribute('data-p2other','1');return '[data-p2other=\"1\"]';}}return '';})()")
                if sel:
                    return sel if real_click("document.querySelector(%r)" % sel) else ""
            return ""

        def scrape_number():
            """Read the 'enter this number' MFA value (2-3 digit leaf text node)."""
            return ev(r"""(function(){var o=[];[].slice.call(document.querySelectorAll('*'))
                .forEach(function(e){var t=(e.textContent||'').trim();
                if(/^\d{2,3}$/.test(t)&&e.children.length===0)o.push(t);});return (o[0]||'');})()""") or ""

        def current_url():
            return (ev("(location&&location.href||'')") or "")

        def wait_for_change(secs, settle_floor=0.4):
            """EVENT-WAIT (AUTH_DEVICE_LOGIN #13 / SESSION_FOLLOWUPS #6): treat
            ``secs`` as a CEILING, not a fixed sleep. Poll for a URL change OR
            document.readyState==='complete' and return as soon as the page has
            observably advanced (after a small ``settle_floor`` so a redirect can
            begin), else wait out the full budget — preserving the old safety
            margin for an in-place SPA update that changes nothing observable.
            Returns the seconds actually waited. Used by the mfa/processing poll
            branches so a human approval (which flips the URL) is picked up within
            ~0.2s instead of always paying the fixed 3s/2.5s inter-poll."""
            u0 = current_url()
            start = _t.time()
            deadline = start + float(secs)
            while _t.time() < deadline:
                _t.sleep(0.2)
                try:
                    changed = (current_url() or "") != (u0 or "")
                    ready = ev("(document.readyState==='complete')")
                except Exception:  # noqa: BLE001
                    changed = ready = False
                if _t.time() - start >= settle_floor and (changed or ready):
                    break
            return round(_t.time() - start, 2)

        def approve_if_present():
            """Device-code 'Approve sign-in on another device' interstitial: Entra
            shows #idBtn_Approve (confirm THIS sign-in) alongside #idBtn_Cancel
            ('This wasn't me'). This is a CLICK gate, not a phone-only approval —
            click Approve. NEVER click Cancel. Returns 'approved' if clicked, else
            '' (no such button → it's a genuine phone/number approval to wait on)."""
            return ev(
                "(function(){var a=document.querySelector('#idBtn_Approve');"
                "if(a&&a.offsetParent!==null){a.click();return 'approved';}"
                "return '';})()") or ""

        def control_shape():
            """STRUCTURAL shape of the visible controls — NO text, NO values. Each
            entry is {tag, type, id, name}: enough to add/fix a selector in a case,
            nothing that could leak an email/password. Used only for diagnosing an
            unmatched screen in the run trace (2b: structure-only feedback)."""
            js = ("(function(){function vis(e){return e&&e.offsetParent!==null;}"
                  "var out=[];var els=[].slice.call(document.querySelectorAll("
                  "'input,button,a[role=button],[data-test-id]'));"
                  "for(var i=0;i<els.length&&out.length<30;i++){var e=els[i];if(!vis(e))continue;"
                  "var ty=(e.getAttribute&&e.getAttribute('type'))||'';"
                  "out.push({tag:(e.tagName||'').toLowerCase(),type:ty,"
                  "id:e.id||'',name:(e.getAttribute&&e.getAttribute('name'))||'',"
                  "tid:(e.getAttribute&&e.getAttribute('data-test-id'))?'y':'' });}"
                  "return JSON.stringify(out);})()")
            raw = ev(js)
            try:
                return _json.loads(raw) if raw else []
            except Exception:  # noqa: BLE001
                return []

        # Precompute the distinct case selectors to probe each tick (for
        # selectors_seen), so the combined scan can check them in one eval.
        _probe_sels = []
        for _sc in case.get("screens", []):
            _w = _sc.get("when", {}) or {}
            for _k in ("selector", "not_selector"):
                _s = _w.get(_k)
                if _s and _s not in _probe_sels:
                    _probe_sels.append(_s)

        def tick_scan():
            """ONE eval per tick (perf #20): returns {text, selectors, dom} —
            body innerText (bounded to 4k, enough for classify text matches),
            per-case-selector presence booleans, and the structural DOM snapshot.
            Replaces 3 separate evals (page_text + selectors_seen + dom_snapshot)."""
            sels_js = _json.dumps(_probe_sels)
            js = ("(function(){"
                  "function vis(e){return e&&e.offsetParent!==null;}"
                  "function sel(e){if(e.id)return '#'+e.id;"
                  "var n=e.getAttribute&&e.getAttribute('name');if(n)return e.tagName.toLowerCase()+'[name=\"'+n+'\"]';"
                  "var ty=e.getAttribute&&e.getAttribute('type');if(ty)return e.tagName.toLowerCase()+'[type=\"'+ty+'\"]';"
                  "return e.tagName.toLowerCase();}"
                  "var txt=(document.body&&document.body.innerText||'').slice(0,4000);"
                  "var probes=%s;var seen={};for(var i=0;i<probes.length;i++){seen[probes[i]]=!!document.querySelector(probes[i]);}"
                  "var out=[];var els=[].slice.call(document.querySelectorAll("
                  "'input,button,a[role=button],[data-test-id]'));"
                  "for(var j=0;j<els.length&&out.length<24;j++){var e=els[j];if(!vis(e))continue;"
                  "out.push({tag:e.tagName.toLowerCase(),type:(e.getAttribute&&e.getAttribute('type'))||'',"
                  "id:e.id||'',name:(e.getAttribute&&e.getAttribute('name'))||'',"
                  "tid:(e.getAttribute&&e.getAttribute('data-test-id'))?'y':'',sel:sel(e)});}"
                  "return JSON.stringify({text:txt,selectors:seen,dom:out});})()" % sels_js)
            raw = ev(js)
            try:
                v = _json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                v = {}
            return {"text": v.get("text", ""), "selectors": v.get("selectors", {}),
                    "dom": v.get("dom", [])}

        def _selector_present(selector):
            if not selector:
                return False
            return bool(ev("(function(){return !!document.querySelector(%r);})()" % selector))

        def resolve_one(selector):
            """Resolve a COMPOUND case selector (e.g. '#i0118,#passwordInput,
            input[type=password]') to the CONCRETE single element actually
            present — returns its canonical selector (#id, else [name=..], else
            [type=..], else tag). This is what makes a step REPLAYABLE: the trace
            records the exact element acted on, not the OR-list pattern."""
            if not selector:
                return ""
            return ev("(function(){var e=document.querySelector(%r);if(!e)return '';"
                      "if(e.id)return '#'+e.id;"
                      "var n=e.getAttribute&&e.getAttribute('name');"
                      "if(n)return e.tagName.toLowerCase()+'[name=\"'+n+'\"]';"
                      "var t=e.getAttribute&&e.getAttribute('type');"
                      "if(t)return e.tagName.toLowerCase()+'[type=\"'+t+'\"]';"
                      "return e.tagName.toLowerCase();})()" % selector) or ""

        def extract_value(selector, attr="", all_=False, count=False):
            """Read text (or an attribute) of element(s) matching `selector` for a
            data-GATHER case. all_=True → list of all matches; count=True → just
            the number of matches; attr=<name> → that attribute instead of text.
            Returns a JSON-safe value (str | list[str] | int)."""
            a = _json.dumps(attr or "")
            if count:
                return ev("(function(){return document.querySelectorAll(%r).length;})()"
                          % selector) or 0
            if all_:
                raw = ev("(function(){var a=%s;var out=[];"
                         "[].slice.call(document.querySelectorAll(%r)).forEach(function(e){"
                         "var v=a?(e.getAttribute(a)||''):(e.textContent||'').trim();"
                         "if(v)out.push(v);});return JSON.stringify(out.slice(0,200));})()"
                         % (a, selector))
                try:
                    return _json.loads(raw) if raw else []
                except Exception:  # noqa: BLE001
                    return []
            return ev("(function(){var a=%s;var e=document.querySelector(%r);if(!e)return '';"
                      "return a?(e.getAttribute(a)||''):(e.textContent||'').trim();})()"
                      % (a, selector)) or ""

        def classify_screen(low, selmap=None):
            """Walk the CASE screens top-to-bottom; return the FIRST whose `when`
            matches (text and/or a selector). `selmap` is the pre-scanned
            selector-presence map from tick_scan (perf #20) — use it instead of a
            live per-selector eval. A `not_selector` present SUPPRESSES the screen.
            Returns the screen dict + its verdict, or (None, '')."""
            def present(sel):
                if selmap is not None and sel in selmap:
                    return bool(selmap[sel])
                return _selector_present(sel)
            _cur = (current_url() or "").lower()
            for sc in case.get("screens", []):
                when = sc.get("when", {}) or {}
                # suppression guard first
                nsel = when.get("not_selector")
                if nsel and present(nsel):
                    continue
                hit = False
                if when.get("any"):
                    hit = True
                else:
                    # URL match is the STRONGEST signal (survives iframe content
                    # that never surfaces in the top-frame text/selectors — e.g.
                    # the reCAPTCHA challenge renders in an iframe but the page
                    # URL is /signin/challenge/recaptcha).
                    ua = when.get("url_any")
                    if ua and any(str(s).lower() in _cur for s in ua):
                        hit = True
                    ta = when.get("text_any")
                    if not hit and ta and any(s.lower() in low for s in ta):
                        hit = True
                    tall = when.get("text_all")
                    if not hit and tall and all(s.lower() in low for s in tall):
                        hit = True
                    if not hit and when.get("selector") and present(when["selector"]):
                        hit = True
                if hit:
                    return sc, (sc.get("classify") or "continue")
            return None, ""

        def run_actions(screen):
            """Execute a screen's `do` action list (with ${...} substitution).
            Returns (acted, typed_secret) where `acted` is a list of STRUCTURAL
            action records {type, selector} naming the EXACT object each step
            acted on — so the step is REPLAYABLE (e.g. click_tile records the
            concrete [data-test-id="…"] it clicked, not the fuzzy match rule)."""
            acted = []
            typed_secret = False
            for act in screen.get("do", []) or []:
                typ = act.get("type")
                if typ == "fill":
                    sel = _subst(act.get("selector", ""))
                    concrete = resolve_one(sel) or sel   # exact element acted on
                    fill_value(sel, _subst(act.get("value", "")))
                    acted.append({"type": typ, "selector": concrete, "matched": sel})
                elif typ == "fill_if":
                    # value-shape guard: 'email' => only fill when it contains '@'
                    # (the web-login "never type a bare username" safety).
                    v = _subst(act.get("value", ""))
                    if act.get("shape", "") == "email" and "@" not in v:
                        acted.append({"type": typ, "selector": "", "skipped": "shape"})
                        continue
                    if v:
                        sel = _subst(act.get("selector", ""))
                        concrete = resolve_one(sel) or sel
                        fill_value(sel, v)
                        acted.append({"type": typ, "selector": concrete, "matched": sel})
                elif typ == "type_secret":
                    pw = secrets.get(act.get("ref", ""), "")
                    if pw:
                        sel = _subst(act.get("selector", "input[type=password]"))
                        concrete = resolve_one(sel) or sel   # the EXACT field typed into
                        type_secret(sel, pw)
                        typed_secret = True
                        # record the concrete element + which compound pattern it
                        # matched; the secret VALUE is never recorded.
                        acted.append({"type": typ, "selector": concrete, "matched": sel,
                                      "ref": act.get("ref", "")})
                elif typ == "click_primary":
                    sel = click_next()        # returns the exact affirmative-button selector
                    acted.append({"type": typ, "selector": sel or ""})
                elif typ == "click":
                    # general trusted click of an arbitrary element (gather/interaction).
                    sel = _subst(act.get("selector", ""))
                    concrete = resolve_one(sel) or sel
                    real_click("document.querySelector(%r)" % sel)
                    acted.append({"type": typ, "selector": concrete, "matched": sel})
                elif typ == "navigate":
                    url = _subst(act.get("url", ""))
                    if url:
                        try:
                            cdp.open_tab(url)   # drive to the target page
                        except Exception:  # noqa: BLE001
                            pass
                        _t.sleep(float(act.get("settle") or 2))
                    acted.append({"type": typ, "url": url})
                elif typ == "extract":
                    # DATA GATHER: read text/attr of element(s) into `collected`.
                    nm = act.get("name") or act.get("selector", "")
                    val = extract_value(_subst(act.get("selector", "")),
                                        attr=act.get("attr", ""),
                                        all_=bool(act.get("all")),
                                        count=bool(act.get("count")))
                    collected[nm] = val
                    n = (len(val) if isinstance(val, list) else (1 if val != "" else 0))
                    acted.append({"type": typ, "name": nm,
                                  "selector": _subst(act.get("selector", "")), "got": n})
                elif typ == "click_tile":
                    sel = click_tile(_subst(act.get("match", "")), bool(act.get("other_tile")))
                    acted.append({"type": typ, "selector": sel or ""})
                elif typ == "capture_cookies":
                    # CREDENTIAL CAPTURE (#29): once signed in, export the session
                    # cookies for the given url(s) into `collected` under `name`.
                    # Reuses the cap's get_cookies_for (reads HttpOnly too). A
                    # domain_filter (comma-list of substrings) keeps only matching
                    # cookie domains; sentinel (optional) gates on its presence.
                    nm = act.get("name") or "cookies"
                    urls = act.get("urls") or ([act["url"]] if act.get("url") else [])
                    urls = [_subst(u) for u in urls if u]
                    got = {}
                    if urls:
                        try:
                            r = cdp.get_cookies_for(tgt, urls)
                            got = (r or {}).get("cookies", {}) if isinstance(r, dict) else {}
                        except Exception:  # noqa: BLE001
                            got = {}
                    filt = [f.strip().lstrip(".") for f in
                            (act.get("domain_filter", "") or "").split(",") if f.strip()]
                    # get_cookies_for returns {name:value}; domain filtering needs
                    # the raw domains, so when a filter is set we re-read with the
                    # richer shape if available, else keep all (the cap already
                    # scoped by url). sentinel presence gate:
                    sentinel = act.get("sentinel", "")
                    if sentinel and sentinel not in got:
                        acted.append({"type": typ, "name": nm, "got": 0,
                                      "note": "sentinel %r absent" % sentinel})
                    else:
                        if filt:
                            # best-effort name-substring filter is meaningless;
                            # domains aren't in the {name:value} shape, so keep all
                            # (url scoping already applied). Recorded for provenance.
                            pass
                        collected[nm] = got
                        acted.append({"type": typ, "name": nm, "got": len(got)})
                elif typ == "capture_bearer":
                    # CREDENTIAL CAPTURE (#29): watch the page's XHR traffic for an
                    # Authorization: Bearer on a host matching host_filter and put
                    # the token into `collected` under `name`.
                    nm = act.get("name") or "bearer"
                    try:
                        r = cdp.capture_network(
                            tgt, url_substr=_subst(act.get("host_filter", "") or ""),
                            header="authorization",
                            duration_s=int(act.get("duration_s") or 20),
                            navigate_url=_subst(act.get("navigate_url", "") or ""))
                        val = (r or {}).get("value") if isinstance(r, dict) else None
                        tok = ""
                        if isinstance(val, str) and val.lower().startswith("bearer "):
                            tok = val[len("bearer "):]
                        elif isinstance(val, str):
                            tok = val
                        collected[nm] = tok
                        acted.append({"type": typ, "name": nm, "got": 1 if tok else 0})
                    except Exception:  # noqa: BLE001
                        acted.append({"type": typ, "name": nm, "got": 0})
                elif typ == "scrape_number":
                    acted.append({"type": typ})   # read elsewhere (mfa handling)
                elif typ == "notify":
                    acted.append({"type": typ})   # handled by the human-gate path
                elif typ == "wait":
                    # EVENT-WAIT (AUTH_DEVICE_LOGIN #13): the declared `seconds`
                    # is a CEILING, not a fixed sleep. After a click_primary the
                    # next Entra screen usually arrives in <1s; poll for a URL
                    # change OR document.readyState==='complete' and return as
                    # soon as the page has advanced, so a run completes in seconds
                    # instead of paying the full 4s/3s each step. Falls back to the
                    # full budget if nothing observably changes (e.g. an in-place
                    # SPA update), preserving the old safety margin.
                    _secs = float(act.get("seconds") or 1)
                    _u0 = current_url()
                    _deadline = _t.time() + _secs
                    _waited = 0.0
                    while _t.time() < _deadline:
                        _t.sleep(0.2)
                        _waited = round(_t.time() - (_deadline - _secs), 2)
                        try:
                            _changed = (current_url() or "") != (_u0 or "")
                            _ready = ev("(document.readyState==='complete')")
                        except Exception:  # noqa: BLE001
                            _changed = False
                            _ready = False
                        # A small floor (0.4s) lets a redirect begin before we
                        # declare 'ready'; then exit on either a URL change or a
                        # settled document.
                        if _t.time() - (_deadline - _secs) >= 0.4 and (_changed or _ready):
                            break
                    acted.append({"type": typ, "seconds": _secs, "waited": _waited})
            return acted, typed_secret

        def click_next():
            # Real (trusted) mouse click so the form POSTs (a scripted .click() is
            # replayed as a GET -> AADSTS900561). CRITICAL: click only the PRIMARY
            # affirmative control — #idSIButton9 ('Sign in'/'Next'/'Yes') or an
            # Accept — and NEVER a Back/No/Cancel/'sign in another way' (clicking
            # those makes Entra report AADSTS90135 'user declined'). We resolve the
            # affirmative button's selector in JS, excluding negatives, then click it.
            js = ("(function(){"
                  "function aff(b){if(!b||b.offsetParent===null)return false;"
                  "var t=((b.value||b.textContent||b.id||'')+'').toLowerCase();"
                  "if(/back|cancel|no,|no thanks|another way|different|don't|do not|decline|close/.test(t))return false;"
                  "return true;}"
                  "var p=document.querySelector('#idSIButton9');if(aff(p))return '#idSIButton9';"
                  "var a=document.querySelector('#idBtn_Accept');if(aff(a))return '#idBtn_Accept';"
                  "var subs=[].slice.call(document.querySelectorAll('input[type=submit],button[type=submit],button'));"
                  "for(var i=0;i<subs.length;i++){var b=subs[i];if(!aff(b))continue;"
                  "var t=((b.value||b.textContent||'')+'').toLowerCase();"
                  "if(/sign in|next|yes|continue|accept|approve|submit/.test(t)){"
                  "if(b.id)return '#'+b.id; b.setAttribute('data-p2click','1');return '[data-p2click=\"1\"]';}}"
                  "return '';})()")
            sel = ev(js)
            if sel:
                if real_click("document.querySelector(%r)" % sel):
                    return sel
            return ""

        def capture_shot():
            """Opt-in per-run screenshot (base64 jpeg). Reuses ONE dedicated WS
            for the whole run (perf #21) — Page.captureScreenshot on the target's
            debugger WS. The _shot_ws is lazily opened and closed in finally.
            Returns '' on any failure so a capture hiccup never breaks the drive."""
            try:
                if _shot_ws["ws"] is None:
                    _shot_ws["ws"] = cdp.connect_ws(tgt["webSocketDebuggerUrl"], timeout=10)
                r = cdp.ws_send_recv(_shot_ws["ws"], "Page.captureScreenshot",
                                     {"format": "jpeg", "quality": 40}, timeout=12)
                return (r or {}).get("data", "") if isinstance(r, dict) else ""
            except Exception:  # noqa: BLE001
                # drop a wedged socket so the next tick reconnects.
                try:
                    if _shot_ws["ws"]:
                        _shot_ws["ws"].close()
                except Exception:  # noqa: BLE001
                    pass
                _shot_ws["ws"] = None
                return ""

        # Wait for the device/login page to actually LOAD before classifying —
        # case `defaults.wait_for_selector` (falls back to a short settle). This
        # fixes the "tick 1 was about:blank" regression: the generic loop used to
        # start classifying before the form existed.
        _wsel = defaults.get("wait_for_selector")
        if _wsel:
            for _ in range(20):
                if _selector_present(_wsel):
                    break
                _t.sleep(1)
        else:
            _t.sleep(2)

        want_shot = bool(inputs.get("capture"))
        shots = []

        # --- generic CASE interpreter loop -------------------------------
        # Walk the case screens each tick; the FIRST matching screen's verdict
        # drives control flow, its `do[]` actions run the browser side. This
        # replaces the former hardcoded steps (code/account/password/consent).
        #
        # A STRUCTURAL trace is recorded each tick (NO page text / values — 2b):
        # {t, url_host, screen, verdict, selectors, actions}. Unmatched screens
        # additionally capture control_shape() so the automator can diagnose +
        # propose a new/fixed case screen. This is the feedback half of the loop.
        # When inputs.capture is set, a per-screen screenshot is also collected.
        state, detail, final = "pending", "", ""
        handed_off = False
        pw_tried = False
        _human_detail = [""]   # last needs_human screen detail (reCAPTCHA/2SV/…)
        done_screens = set()   # screens whose actions already ran (fire-once)
        trace = []
        unmatched = []
        wait_streak = [0]      # consecutive 'wait' ticks (diagnostic only)
        resend_streak = {"n": 0}   # #9: bounded auto 'send another request' clicks
        repeat_streak = {"screen": "", "n": 0}   # stuck-loop detector: a DONE
        # screen re-matching over and over (no progress) means the flow is wedged
        # (e.g. an expired/used device code bounces back to code_entry). Break out
        # with a terminal verdict instead of idling until the human budget.
        t0 = _t.time()

        def _host(u):
            try:
                import urllib.parse as _up
                return _up.urlsplit(u).netloc + _up.urlsplit(u).path
            except Exception:  # noqa: BLE001
                return ""

        _cur_shot = [None]   # screenshot captured at classify-time for THIS tick
        _cur_sel = [None]    # selectors_seen() captured at classify-time
        _cur_dom = [None]    # dom_snapshot() captured at classify-time
        _last_shot_sig = [None]   # #25: (name, verdict) of the last captured frame

        def _tick(name, verdict, actions, shot_img=None):
            if len(trace) < 60:
                # Use the classify-time DOM/selector snapshots (NOT a fresh eval
                # here, which runs post-action after the page navigated and would
                # wrongly report the screen's own selector as absent).
                entry = {"t": round(_t.time() - t0, 1), "url": _host(current_url()),
                         "screen": name or "", "verdict": verdict or "none",
                         "selectors": _cur_sel[0] if _cur_sel[0] is not None else {},
                         "dom": _cur_dom[0] if _cur_dom[0] is not None else [],
                         "actions": actions or []}
                # Attach the screenshot CAPTURED AT CLASSIFY TIME (this iteration's
                # _cur_shot), so the image matches the labeled screen — NOT a fresh
                # grab here, which would be post-action (page already navigated)
                # and lag the label by one step.
                img = shot_img if shot_img is not None else _cur_shot[0]
                # ONE frame per DISTINCT screen: skip capturing if this screen
                # already has a frame anywhere in the filmstrip (the mfa poll
                # re-ticks the same 'mfa' screen every ~3s — we keep just the
                # first). Point the tick at the existing frame so the trace still
                # references an image.
                existing = next((s for s in shots if s.get("screen") == (name or "")
                                 and (name or "")), None)
                if existing is not None:
                    entry["shot"] = existing["i"]
                elif want_shot and img and len(shots) < 30:
                    idx = len(shots)
                    shots.append({"i": idx, "t": entry["t"], "screen": entry["screen"],
                                  "img": img})
                    entry["shot"] = idx
                trace.append(entry)

        while _t.time() < deadline:
            # TAB-IDENTITY REVALIDATION (AUTH_DEVICE_LOGIN #11): a mid-flow
            # redirect (SAS/ProcessAuth → reprocess → service) can replace the
            # tab's debugger target, leaving the cached tgt's webSocketDebuggerUrl
            # stale → evals/clicks silently hit a dead socket and the driver
            # desyncs. Re-resolve the target by its STABLE tab_id each tick; if
            # the WS url moved, mutate tgt IN PLACE (every closure reads tgt live,
            # so an in-place update refreshes ev/_cdp/capture_shot) and drop the
            # cached input/shot sockets so the next use reconnects to the fresh
            # target. Best-effort — a lookup miss leaves the current tgt as-is.
            try:
                _fresh = cdp.target_by_id(tab_id)
                if _fresh and _fresh.get("webSocketDebuggerUrl") \
                        and _fresh["webSocketDebuggerUrl"] != tgt.get("webSocketDebuggerUrl"):
                    tgt.clear()
                    tgt.update(_fresh)
                    for _wsh in (_iws, _shot_ws):
                        try:
                            if _wsh["ws"]:
                                _wsh["ws"].close()
                        except Exception:  # noqa: BLE001
                            pass
                        _wsh["ws"] = None
            except Exception:  # noqa: BLE001
                pass
            # ONE combined scan per tick (perf #20): text + selector-presence + dom.
            _scan = tick_scan()
            final = _scan["text"]
            _cur_sel[0] = _scan["selectors"]
            _cur_dom[0] = _scan["dom"]
            screen, verdict = classify_screen(final.lower(), _scan["selectors"])
            name = (screen or {}).get("name", "")
            det = (screen or {}).get("detail", "")
            # Screenshot still grabbed at classify-time (before actions) so the
            # trace reflects the page AS CLASSIFIED. Skip if this screen already
            # has a frame (the mfa poll would otherwise re-capture every tick).
            # #25: dedup also covers UNMATCHED/unnamed screens — a long wait on an
            # unmatched or processing page (name=="") previously JPEG-encoded a
            # shot EVERY tick because the name-keyed _have_screen was always False
            # for an empty name. Gate on the (name, verdict) signature too so an
            # unchanged screen (named or not) is captured ONCE, not per tick.
            _sig = (name or "", verdict or "")
            _have_screen = (_last_shot_sig[0] == _sig) or any(
                s.get("screen") == (name or "") and (name or "") for s in shots)
            _do_capture = want_shot and not _have_screen and len(shots) < 30
            _cur_shot[0] = capture_shot() if _do_capture else None
            if _do_capture and _cur_shot[0]:
                _last_shot_sig[0] = _sig
            if verdict != "wait":
                wait_streak[0] = 0
            if verdict in ("denied", "completed", "collected"):
                # ROUND3 #7: a terminal screen's READ-ONLY `do` steps (credential
                # capture / gather) still run before the loop ends - the google
                # 'completed' screen's capture_cookies was declared but never
                # executed, so `collected` stayed empty on a signed-in run. Only
                # capture/extract types: nothing clicks or types on a final page.
                _term_acted = []
                if verdict != "denied" and (screen or {}).get("do"):
                    _ro = [a for a in screen.get("do") or [] if isinstance(a, dict)
                           and a.get("type") in ("capture_cookies", "capture_bearer", "extract")]
                    if _ro:
                        try:
                            _term_acted, _ = run_actions(dict(screen, do=_ro))
                        except Exception as _e:  # noqa: BLE001
                            _term_acted = [{"type": "error", "note": str(_e)[:200]}]
                _tick(name, verdict, _term_acted)
                state, detail = verdict, det
                break
            if verdict == "wait":
                # A 900561 / redirect-processing page. Settle + re-read; NEVER
                # click or submit here — ProcessAuth self-submits its own POST and
                # any interference turns it into a GET (the 900561 dead-end). If it
                # genuinely can't recover, it ages out to needs_human, not a
                # self-inflicted error.
                wait_streak[0] += 1
                _tick(name, verdict, [])
                wait_for_change(2.5)   # #6: ProcessAuth self-POST completes → URL change; exit early
                continue
            if verdict == "password":
                # OPTION A (Google blocks an AUTOMATED password submit as an
                # insecure browser): FILL the password via trusted keystrokes
                # (type_secret) but do NOT auto-submit. Then HAND OFF to the human
                # in the viewer — the user clicks "Next" themselves (a trusted,
                # user-originated click that Google doesn't flag like a scripted
                # submit). The case's `do` for the password screen must therefore
                # contain ONLY type_secret (no click/click_primary).
                if name not in done_screens:
                    done_screens.add(name)
                    if not pw_tried:
                        _acted, _typed = run_actions(screen)   # fills the password
                        pw_tried = True
                        _tick(name, verdict, _acted + ["handoff-to-viewer"])
                # keep the tab OPEN; return to the orchestrator so the viewer opens
                # with the password pre-filled for the user to click Next.
                human_pending[0] = True
                handed_off = True
                state = "needs_human"
                detail = "Password entered — click Next in the viewer to continue."
                _human_detail[0] = detail
                break
            if verdict == "needs_human":
                # A step ONLY the user can do IN THE PAGE — reCAPTCHA ("Confirm
                # you're not a robot"), a 2SV challenge selection, an identity
                # verification prompt. Do NOT click/submit anything; keep the tab
                # OPEN (human_pending) so the viewer can attach + the user completes
                # it. Poll for the page to advance (solving it redirects). Ages
                # out to needs_human if the human budget expires.
                #
                # NOTE: do NOT post the Entra _notify_mfa here — that is the
                # Microsoft "Authenticator approval" notification and is WRONG for
                # a reCAPTCHA / generic challenge. The ORCHESTRATOR (e.g. gkeep)
                # owns the user-facing notification with the correct screen detail
                # (it reads this run's `detail`/`state`). The daemon only needs to
                # keep the tab alive + record the detail.
                human_pending[0] = True   # signal finally: DO NOT close the tab
                # Return to the orchestrator IMMEDIATELY (do NOT loop here for the
                # whole human budget) — gkeep must open the viewer dialog NOW so
                # the user can see + solve the challenge. Blocking here for 180s
                # was why the reCAPTCHA screen never mirrored: device_login didn't
                # return the tab_id until it timed out. The viewer drives the tab
                # from here on; gkeep collects when the session lands on Keep.
                handed_off = True
                state = "needs_human"
                detail = det or _human_detail[0] or "a verification step is required"
                _human_detail[0] = detail
                _tick(name, verdict, ["await-human", "handoff-to-viewer"])
                break
            if verdict == "mfa":
                # TWO kinds of 'mfa' gate:
                #  (a) device-code "Approve sign-in on another device" — a CLICK
                #      gate with #idBtn_Approve. We CLICK Approve (never Cancel).
                #  (b) phone/number approval — ONLY the user can do this, on the
                #      phone. Read the number, notify, keep the tab OPEN, poll.
                # Try (a) first: if an Approve button is present, this is the
                # device-code confirm and clicking it advances the flow.
                approved = approve_if_present()
                if approved:
                    human_pending[0] = True
                    _tick(name, verdict, [{"type": "click", "selector": approved}])
                    wait_for_change(3)   # #6: human/redirect advances the URL — exit early
                    continue
                # AUTO-RESEND (AUTH_DEVICE_LOGIN #9): a denied/expired/unanswered
                # push shows "Request denied" / "We didn't hear from you" / "didn't
                # receive" with a primary "Send another request" button. Instead of
                # idling until the human budget on a dead request, CLICK that button
                # (trusted click), then fall through to re-scrape the NEW number +
                # re-notify. Guard with a bounded streak so we don't click forever
                # if it never clears (then let it age out to needs_human).
                _denied_push = any(p in final.lower() for p in
                                   ("request denied", "we didn't hear from you",
                                    "we didn't receive", "didn't receive a request",
                                    "request timed out", "we couldn't verify",
                                    "weren't able to verify", "try again"))
                if _denied_push and resend_streak["n"] < 3:
                    _rs = ev(
                        "(function(){var bs=[].slice.call(document.querySelectorAll("
                        "'a,button,input[type=submit],input[type=button]'));"
                        "for(var i=0;i<bs.length;i++){var t=((bs[i].value||bs[i].textContent"
                        "||'')+'').toLowerCase();if(bs[i].offsetParent!==null&&"
                        "/send another request|send a new request|resend|try again|send request/"
                        ".test(t)){if(bs[i].id)return '#'+bs[i].id;"
                        "bs[i].setAttribute('data-p2resend','1');return '[data-p2resend=\"1\"]';}}"
                        "return '';})()")
                    if _rs and real_click("document.querySelector(%r)" % _rs):
                        resend_streak["n"] += 1
                        human_pending[0] = True
                        _tick(name, verdict, [{"type": "click", "selector": _rs,
                                               "detail": "send-another-request"}])
                        wait_for_change(3)   # #6: the resend re-renders a new number — exit on change
                        continue
                # (b) genuine phone/number approval — never click; notify + wait.
                num = scrape_number()
                human_pending[0] = True   # signal finally: DO NOT close the tab
                now = _t.time()
                acts = ["scrape_number"]
                if not handed_off:
                    handed_off = True
                    _notify_mfa(uri, code, ctx, number=num)
                    last_notify[0] = now
                    acts.append("notify")
                    deadline = now + human_budget
                elif num and (now - last_notify[0]) > 25:
                    _notify_mfa(uri, code, ctx, number=num)   # nudge again with the number
                    last_notify[0] = now
                    acts.append("notify")
                _tick(name, verdict, acts)
                wait_for_change(3)   # #6: the user approving on their phone flips the URL — exit early
                continue
            # verdict == 'continue' (a known actionable screen) OR no match.
            if screen and name not in done_screens:
                done_screens.add(name)
                repeat_streak["screen"] = ""   # real progress — reset stuck detector
                repeat_streak["n"] = 0
                _acted, _ = run_actions(screen)
                _tick(name, verdict, _acted)   # records the EXACT selector each step acted on
                _t.sleep(1)
                continue
            # unrecognized / already-handled screen.
            # CRITICAL: NEVER click on a redirect-PROCESSING page (SAS/ProcessAuth,
            # /reprocess, /kmsi auto-submit). These pages self-submit a POST form
            # via onload; a trusted click here navigates them as a GET → the
            # AADSTS900561 dead-end. On such URLs just WAIT and let Entra's own JS
            # complete the POST. (This was the self-inflicted 900561 the trace
            # revealed: the unmatched→click_primary fallback hammering ProcessAuth.)
            cur = (current_url() or "").lower()
            is_processing = any(k in cur for k in
                                ("/sas/processauth", "/reprocess", "/kmsi", "/processauth"))
            # An 'Approve sign-in on another device' interstitial can land on a
            # ProcessAuth URL. If its Approve button is present, CLICK it (the
            # device-code confirm) — this is the step that completes the sign-in.
            if is_processing:
                _ap = approve_if_present()
                if _ap:
                    human_pending[0] = True
                    _tick(name or "", "mfa", [{"type": "click", "selector": _ap}])
                    wait_for_change(3)   # #6: ProcessAuth approve → redirect; exit on URL change
                    continue
            # If a number-match MFA is being shown on a processing page (text
            # didn't match an mfa phrase, but a 2-3 digit code is displayed),
            # treat it as a HUMAN gate: scrape the number, NOTIFY, and WAIT for
            # the user to enter it in Authenticator — never click/submit.
            if is_processing:
                pnum = scrape_number()
                if pnum:
                    human_pending[0] = True
                    now2 = _t.time()
                    if not handed_off:
                        handed_off = True
                        _notify_mfa(uri, code, ctx, number=pnum)
                        last_notify[0] = now2
                        deadline = now2 + human_budget
                    elif (now2 - last_notify[0]) > 25:
                        _notify_mfa(uri, code, ctx, number=pnum)
                        last_notify[0] = now2
                    _tick(name or "", "mfa", ["scrape_number", "notify"])
                    wait_for_change(3)   # #6: human number entry flips the URL — exit early
                    continue
            if not screen:
                shape = control_shape()
                if len(unmatched) < 10:
                    unmatched.append({"t": round(_t.time() - t0, 1),
                                      "url": _host(current_url()), "controls": shape})
                _clicked = "" if is_processing else (click_next() or "")
                _tick("", "wait" if is_processing else "unmatched",
                      [] if is_processing else [{"type": "click", "selector": _clicked}])
            else:
                # A DONE screen re-matched (repeat). Count consecutive repeats of
                # the SAME screen; if it keeps bouncing back with no progress the
                # flow is wedged (e.g. an expired/already-used device code returns
                # to code_entry) — break with a terminal verdict rather than idle.
                if repeat_streak["screen"] == name:
                    repeat_streak["n"] += 1
                else:
                    repeat_streak["screen"] = name
                    repeat_streak["n"] = 1
                if not is_processing and repeat_streak["n"] >= 6:
                    _tick(name, "stuck", [])
                    state = "denied"
                    detail = ("stuck re-matching %r (no progress) — likely an "
                              "expired/used code or a selector that won't advance" % name)
                    break
                _clicked = "" if is_processing else (click_next() or "")
                _tick(name, "repeat",
                      [] if is_processing else [{"type": "click", "selector": _clicked}])
            wait_for_change(2.5)   # #6: unmatched/processing/repeat poll — exit as soon as the page advances
        if state == "pending":
            state = "needs_human" if handed_off else "pending"
            detail = detail or (_human_detail[0] or "waiting for you to finish the sign-in"
                                if handed_off else "no terminal screen reached")
        # Resolve the persistent MFA notification to its terminal lifecycle state
        # so it stops showing as pending (proceeded or invalidated).
        if handed_off or human_pending[0]:
            if state == "completed":
                _resolve_mfa("resolved")
            elif state == "denied":
                _resolve_mfa("denied")
            else:
                _resolve_mfa("expired")   # needs_human / pending / timed out
        _mfa_num = ""
        if human_pending[0]:
            try:
                _mfa_num = scrape_number()
            except Exception:  # noqa: BLE001
                _mfa_num = ""
        return {"ok": True, "state": state, "detail": detail, "case": case.get("id", ""),
                "schema_version": _INTERP_SCHEMA_VERSION,
                "mfa_number": _mfa_num, "human_pending": human_pending[0],
                "tab_id": tab_id,
                "trace": trace, "unmatched": unmatched, "shots": shots,
                "collected": collected,
                "final_text": (final or "")[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    finally:
        try:
            if _iws.get("ws") is not None:
                _iws["ws"].close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if _shot_ws.get("ws") is not None:
                _shot_ws["ws"].close()
        except Exception:  # noqa: BLE001
            pass
        # Keep the tab OPEN if an Authenticator approval is pending — closing it
        # kills the live approve request (the bug that discarded the number the
        # user was about to enter). Only close on a clean finish or when asked.
        try:
            if tab and not args.get("keep_tab") and not human_pending[0]:
                cdp.close_tab(tab.get("id"))
        except Exception:  # noqa: BLE001
            pass


def get_cookies(args, ctx):
    """Export the signed-in browser's cookies for the given URL(s) as {name:
    value} — reads HttpOnly session cookies (e.g. ETSI's DNN .DOTNETNUKE) that
    document.cookie/eval cannot see. args: {urls: [str] or url: str}. Returns
    {ok, cookies:{name:value}, count}. The authenticated-download seam for
    3gpp etsi_auth / download_etsi (#42)."""
    bad = cdp.ensure_available()
    if bad:
        return bad
    urls = args.get("urls")
    if not urls and args.get("url"):
        urls = [args["url"]]
    if not urls:
        return {"ok": False, "error": "urls (or url) required", "error_category": "application"}
    try:
        tgt = (cdp.page_tabs() or [None])[0]
        if not tgt or "webSocketDebuggerUrl" not in tgt:
            return {"ok": False, "error": "no debuggable page tab available",
                    "error_category": "unavailable"}
        return cdp.get_cookies_for(tgt, urls)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


def capture_network(args, ctx):
    """Capture a request header (default Authorization/Bearer) from the signed-in
    browser's network traffic over a window — the primitive the 'flow' (Power
    Automate) and similar token plugins need (the Bearer rides API request
    headers, not a cookie/localStorage). Enables the CDP Network domain on a
    page tab, optionally navigates it to kick off the API traffic, then returns
    the FIRST matching request's header value.

    args: url_substr (match request URLs containing this; '' = any),
          header (default 'authorization'), duration_s (default 20),
          navigate_url (optional — navigate the tab first to trigger traffic),
          tab_id (optional — which page tab; default the first).
    Returns {ok, value, url, method} (value None + timed_out if nothing matched).
    """
    bad = cdp.ensure_available()
    if bad:
        return bad
    tab_id = args.get("tab_id")
    try:
        if tab_id:
            tgt = cdp.target_by_id(tab_id)
        elif args.get("navigate_url"):
            t = cdp.open_tab("about:blank")
            tgt = cdp.wait_for_ws(t.get("id"))
        else:
            tgt = (cdp.page_tabs() or [None])[0]
        if not tgt or "webSocketDebuggerUrl" not in tgt:
            return {"ok": False, "error": "no debuggable page tab available",
                    "error_category": "unavailable"}
        return cdp.capture_network(
            tgt, url_substr=args.get("url_substr", "") or "",
            header=args.get("header", "authorization") or "authorization",
            duration_s=int(args.get("duration_s") or 20),
            navigate_url=args.get("navigate_url", "") or "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


def stop_browser(args, ctx):
    """Stop the MANAGED Edge browser to free its resources (RAM/CPU) — the v2
    'lower resource usage' control. Idempotent: a no-op (ok:True) when nothing
    is running. The browser caps relaunch it lazily on the next use, so this is
    safe to call when idle. Only the dedicated managed profile is touched (never
    an externally-attached browser)."""
    try:
        from . import _edge_profile as _edge
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "managed-profile control unavailable: %s" % exc,
                "error_category": "unavailable"}
    was_running = False
    try:
        was_running = _edge.current_mode() is not None
    except Exception:  # noqa: BLE001
        pass
    try:
        _edge.stop()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "edge stop failed: %s" % exc,
                "error_category": "application"}
    # #18: invalidate the cached availability probe — a deliberate stop flips the
    # endpoint dead, so the next cdp op must re-probe (+ relaunch) rather than
    # trust a stale cached OK.
    try:
        cdp.invalidate_available()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "stopped": bool(was_running),
            "detail": "managed Edge stopped" if was_running else "nothing was running"}


# ============================================================================
# Interactive SCREENCAST sign-in (an in-ai-pylon login viewer, headless-first)
# ----------------------------------------------------------------------------
# A caller (e.g. gkeep) opens a sign-in SESSION: we open the login url in the
# managed Edge and run a background Page.captureScreenshot loop so a client can
# MIRROR the page (base64-JPEG frames) in a floating window and RELAY input
# (click/type/key) back via Input.dispatch* — the same engine the remote SSO
# viewer uses (apps/knowledge/workspace/impl/cdp_session.py), but for the LOCAL
# managed Edge, exposed as cap methods.
#
# HEADLESS-FIRST (the chosen design): start headless (no OS window). Google's
# first interactive sign-in often REJECTS headless Chromium ("couldn't sign you
# in / this browser or app may not be secure"); signin_status DETECTS that page
# and reports needs_headful=true, and signin_open{headful:true} relaunches the
# managed Edge HEADFUL (still screencast-mirrored) so the login can complete.
import threading as _sc_threading
import os  # noqa: E402  (module-level for the screencast session; local imports elsewhere)
import time  # noqa: E402

_SIGNIN_SESSIONS = {}          # sid -> _ScreencastSession
_SIGNIN_LOCK = _sc_threading.Lock()
# markers of Google's "insecure browser" / blocked-headless sign-in page
_HEADFUL_MARKERS = (
    "couldn't sign you in", "couldn’t sign you in",
    "this browser or app may not be secure", "browser or app may not be secure",
    "secure browsers help keep", "try using a different browser",
)


class _ScreencastSession:
    """One sign-in tab + a background JPEG capture loop against the managed Edge."""

    def __init__(self, sid, url, headful, profile=None, attach_tab_id=None):
        self.sid = sid
        self.url = url
        self.headful = bool(headful)
        self.profile = profile
        self.attach_tab_id = attach_tab_id   # ATTACH to this existing tab (don't open)
        self.cdp = cdp
        self.tab_id = None
        self._ws_url = None
        self._frame = ""            # latest base64 JPEG
        self._cur_url = url
        self._needs_headful = False
        self._lock = _sc_threading.Lock()
        self._stop = _sc_threading.Event()
        self._thread = None
        self._q = int(os.environ.get("CDP_SIGNIN_JPEG_Q", "45"))
        # push-based Page.startScreencast (frames only on change, acked) instead of
        # polling captureScreenshot at ~5fps; falls back to polling if no frame arrives.
        self._use_screencast = os.environ.get("CDP_SIGNIN_SCREENCAST", "1") != "0"
        self.mode = "poll"
        self._last_input = 0.0
        self._refs = set()          # session ids sharing this capture loop
        self._text = ""             # first ~600 chars of the page text on /challenge/ pages (UX hints)
        self._max_w = int(os.environ.get("CDP_SIGNIN_MAX_W", "1100"))

    def start(self):
        if self.profile:
            from ._browser_profiles import Client
            self.cdp = Client(self.profile, headful=self.headful)
        else:
            from . import _edge_profile as _edge
            dbg = _edge.ensure_mode(headless=not self.headful)
            if not dbg:
                return False
            self.cdp.configure(dbg)
        cdp_client = self.cdp
        # ATTACH to an existing login tab (the one the automated ladder is
        # driving), so the user takes over the SAME page for a verification
        # step — instead of opening a fresh tab that loses the flow. Fall back
        # to opening self.url if the tab vanished.
        if self.attach_tab_id:
            tgt = cdp_client.target_by_id(self.attach_tab_id)
            if tgt and tgt.get("webSocketDebuggerUrl"):
                self.tab_id = self.attach_tab_id
            else:
                self.attach_tab_id = None
        if not self.tab_id:
            t = cdp_client.open_tab(self.url)
            self.tab_id = t.get("id")
            tgt = cdp_client.wait_for_ws(self.tab_id, timeout=10)
        if not tgt:
            return False
        self._ws_url = tgt.get("webSocketDebuggerUrl")
        # cap the device width so frames are smaller + cheaper to ship
        try:
            ws = self.cdp.connect_ws(self._ws_url, timeout=10)
            try:
                self.cdp.ws_send_recv(ws, "Emulation.setDeviceMetricsOverride",
                                 {"width": self._max_w, "height": 900,
                                  "deviceScaleFactor": 1, "mobile": False}, timeout=8)
            finally:
                ws.close()
        except Exception:  # noqa: BLE001
            pass
        self._thread = _sc_threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def _loop(self):
        if self._use_screencast:
            try:
                if self._loop_screencast():
                    return          # stopped normally
            except Exception:  # noqa: BLE001
                pass
            self.mode = "poll"      # screencast unavailable -> poll
        self._loop_poll()

    def _loop_screencast(self):
        """Event-driven capture. Returns True when stopped by stop(), False if the
        screencast never produced a frame (caller falls back to polling)."""
        import json as _j
        tgt = self.cdp.target_by_id(self.tab_id)
        if not tgt or not tgt.get("webSocketDebuggerUrl"):
            return False
        ws = self.cdp.connect_ws(tgt["webSocketDebuggerUrl"], timeout=10)
        try:
            ws.settimeout(1.0)
            ws.send(_j.dumps({"id": 900001, "method": "Page.startScreencast",
                              "params": {"format": "jpeg", "quality": self._q,
                                         "maxWidth": self._max_w, "everyNthFrame": 1}}))
            got = False
            t_start = time.time()
            last_url = 0.0
            eval_id = 910000
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except Exception as exc:  # noqa: BLE001
                    if "timed out" in str(exc).lower() or exc.__class__.__name__ == "WebSocketTimeoutException":
                        raw = None
                    else:
                        raise
                if raw:
                    try:
                        m = _j.loads(raw)
                    except Exception:  # noqa: BLE001
                        m = {}
                    if m.get("method") == "Page.screencastFrame":
                        pr = m.get("params") or {}
                        ws.send(_j.dumps({"id": 900002, "method": "Page.screencastFrameAck",
                                          "params": {"sessionId": pr.get("sessionId")}}))
                        if pr.get("data"):
                            got = True
                            self.mode = "screencast"
                            with self._lock:
                                self._frame = pr["data"]
                    elif m.get("id") == eval_id:
                        _val = str(((m.get("result") or {}).get("result") or {}).get("value") or "")
                        low = _val.lower()
                        with self._lock:
                            self._text = _val[:600] if "/challenge/" in self._cur_url else ""
                        if not self.headful and any(k in low for k in _HEADFUL_MARKERS):
                            with self._lock:
                                self._needs_headful = True
                now = time.time()
                if not got and now - t_start > 4.0:
                    return False
                if now - last_url > 0.5:
                    last_url = now
                    try:
                        cur = (self.cdp.target_by_id(self.tab_id) or {}).get("url", "") or ""
                        if cur:
                            with self._lock:
                                self._cur_url = cur
                    except Exception:  # noqa: BLE001
                        pass
                    if (not self.headful and not self._needs_headful) or "/challenge/" in self._cur_url:
                        eval_id += 1
                        ws.send(_j.dumps({"id": eval_id, "method": "Runtime.evaluate",
                                          "params": {"expression": "document.body?document.body.innerText.slice(0,4000):'",
                                                     "returnByValue": True}}))
            return True
        finally:
            try:
                ws.send(_j.dumps({"id": 900003, "method": "Page.stopScreencast"}))
            except Exception:  # noqa: BLE001
                pass
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _loop_poll(self):
        # a DEDICATED ws for the capture loop (don't share the pooled eval ws).
        ws = None
        gone_since = None
        while not self._stop.is_set():
            try:
                if ws is None:
                    tgt = self.cdp.target_by_id(self.tab_id)
                    if not tgt or not tgt.get("webSocketDebuggerUrl"):
                        # the tab was closed underneath us: end the session (and its
                        # registry entries) instead of polling a dead target forever.
                        gone_since = gone_since or time.time()
                        if time.time() - gone_since > 20:
                            self._reap()
                            return
                        self._stop.wait(0.5)
                        continue
                    gone_since = None
                    ws = self.cdp.connect_ws(tgt["webSocketDebuggerUrl"], timeout=10)
                r = self.cdp.ws_send_recv(ws, "Page.captureScreenshot",
                                     {"format": "jpeg", "quality": self._q}, timeout=12)
                data = (r or {}).get("data", "")
                cur = ""
                try:
                    cur = (self.cdp.target_by_id(self.tab_id) or {}).get("url", "") or ""
                except Exception:  # noqa: BLE001
                    cur = ""
                with self._lock:
                    if data:
                        self._frame = data
                    if cur:
                        self._cur_url = cur
                # detect the headless-blocked page (scan the DOM text cheaply)
                if (not self.headful and not self._needs_headful) or "/challenge/" in self._cur_url:
                    try:
                        txt = self.cdp.ws_send_recv(ws, "Runtime.evaluate",
                            {"expression": "document.body?document.body.innerText.slice(0,4000):''",
                             "returnByValue": True}, timeout=8)
                        _val = str((txt.get("result") or {}).get("value") or "")
                        low = _val.lower()
                        with self._lock:
                            self._text = _val[:600] if "/challenge/" in self._cur_url else ""
                        if not self.headful and any(m in low for m in _HEADFUL_MARKERS):
                            with self._lock:
                                self._needs_headful = True
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 — wedged socket: drop + reopen next tick
                try:
                    if ws:
                        ws.close()
                except Exception:  # noqa: BLE001
                    pass
                ws = None
            # adaptive cadence: fast right after input, backing off when idle
            idle_for = time.time() - self._last_input
            self._stop.wait(0.2 if idle_for < 5 else (0.4 if idle_for < 30 else 0.8))
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _reap(self):
        self._stop.set()
        with _SIGNIN_LOCK:
            for k in [k for k, v in _SIGNIN_SESSIONS.items() if v is self]:
                _SIGNIN_SESSIONS.pop(k, None)

    def shot(self):
        with self._lock:
            return self._frame, self._cur_url, self._needs_headful

    def _layout(self):
        """Live CSS layout viewport {w,h} via Page.getLayoutMetrics — the space
        CDP Input.* coordinates live in (CSS px, NOT device px). Cached briefly.
        Falls back to the captured-frame size guess if the call fails."""
        now = time.time()
        if getattr(self, "_lm", None) and (now - self._lm[0]) < 2.0:
            return self._lm[1]
        vp = {"w": self._max_w, "h": 900}
        try:
            tgt = self.cdp.target_by_id(self.tab_id)
            if tgt and tgt.get("webSocketDebuggerUrl"):
                ws = self.cdp.connect_ws(tgt["webSocketDebuggerUrl"], timeout=8)
                try:
                    r = self.cdp.ws_send_recv(ws, "Page.getLayoutMetrics", {}, timeout=8)
                finally:
                    ws.close()
                lv = (r or {}).get("cssLayoutViewport") or (r or {}).get("layoutViewport") or {}
                if lv.get("clientWidth") and lv.get("clientHeight"):
                    vp = {"w": int(lv["clientWidth"]), "h": int(lv["clientHeight"])}
        except Exception:  # noqa: BLE001
            pass
        self._lm = (now, vp)
        return vp

    def _to_css(self, payload):
        """Map an input payload's coords to CSS layout px. Accepts NORMALIZED
        fractions (xf/yf in 0..1 — the robust path, resolution-independent) or
        absolute x/y (already CSS px). Returns (x, y)."""
        vp = self._layout()
        if "xf" in payload or "yf" in payload:
            return (float(payload.get("xf", 0)) * vp["w"],
                    float(payload.get("yf", 0)) * vp["h"])
        return (float(payload.get("x", 0)), float(payload.get("y", 0)))

    def _cmd(self, method, params):
        return self._cmd_many([(method, params)])

    def _cmd_many(self, events):
        """Send a SEQUENCE of CDP commands on ONE WebSocket. A click's
        move→press→release MUST share a connection — CDP pairs press/release and
        tracks the last mouse position PER SESSION, so splitting them across
        separate sockets (the old per-event _cmd) produced a press+release that
        didn't register as a real click (the 'click from dialog not working'
        bug, esp. for the reCAPTCHA checkbox)."""
        tgt = self.cdp.target_by_id(self.tab_id)
        if not tgt or not tgt.get("webSocketDebuggerUrl"):
            return False
        ws = self.cdp.connect_ws(tgt["webSocketDebuggerUrl"], timeout=10)
        try:
            for method, params in events:
                self.cdp.ws_send_recv(ws, method, params, timeout=10)
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def click(self, payload):
        x, y = self._to_css(payload)
        x, y = int(round(x)), int(round(y))
        # A FULL click sequence on ONE socket: move to the point (reCAPTCHA + many
        # sites gate on a real hover/cursor position), then press + release with a
        # consistent button + clickCount. Splitting these lost the click.
        return self._cmd_many([
            ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 0}),
            ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                          "button": "left", "buttons": 1, "clickCount": 1}),
            ("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                          "button": "left", "buttons": 0, "clickCount": 1}),
        ])

    def type_text(self, text):
        if not text:
            return True
        return self._cmd("Input.insertText", {"text": str(text)})

    def pointer(self, kind, payload):
        x, y = self._to_css(payload)
        params = {"type": "mouseWheel" if kind == "wheel" else "mouseMoved",
                  "x": float(x), "y": float(y)}
        if kind == "wheel":
            params.update(deltaX=float(payload.get("deltaX", 0)), deltaY=float(payload.get("deltaY", 0)))
        return self._cmd("Input.dispatchMouseEvent", params)

    def key(self, key, code=None, modifiers=0):
        virtual = {"Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27,
                   "PageUp": 33, "PageDown": 34, "End": 35, "Home": 36,
                   "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40, "Delete": 46}
        params = {"key": key, "modifiers": int(modifiers)}
        if code:
            params["code"] = code
        vk = virtual.get(key, ord(key.upper()) if len(key) == 1 else 0)
        if vk:
            params["windowsVirtualKeyCode"] = vk
        down = dict(params, type="keyDown")
        if key == "Enter":
            down["text"] = "\r"
        ok = self._cmd("Input.dispatchKeyEvent", down)
        return self._cmd("Input.dispatchKeyEvent", dict(params, type="keyUp")) and ok

    def stop(self):
        self._stop.set()
        # only close a tab WE opened — never close a tab we merely attached to
        # (that's the automation's live login tab; closing it kills the flow).
        if not self.attach_tab_id:
            self.cdp.close_tab(self.tab_id)


def _sc_get(sid):
    with _SIGNIN_LOCK:
        return _SIGNIN_SESSIONS.get(sid)


def signin_open(args, ctx):
    """Open an interactive screencast sign-in SESSION on the managed Edge and
    start a background JPEG capture loop. Headless-first; pass headful=true to
    (re)launch headful when Google blocks the headless login. Pass
    attach_tab_id=<id> to ATTACH to an EXISTING login tab (e.g. the tab the
    automated login left at a verification step) instead of opening a new one —
    so the user takes over the SAME page. Returns {ok, session, headful}."""
    bad = None if args.get('profile') else cdp.ensure_available()
    # ensure_available relaunches headless; for a headful request we force the
    # mode below regardless, so a bad probe here is non-fatal for headful.
    attach_tab_id = (args.get("attach_tab_id") or "").strip() or None
    url = (args.get("url") or "").strip()
    if not url and not attach_tab_id:
        return {"ok": False, "error": "url or attach_tab_id required",
                "error_category": "application"}
    headful = bool(args.get("headful"))
    sid = (args.get("session") or "").strip() or ("si-%d" % int(time.time() * 1000))
    # if re-opening an existing sid (headful upgrade), stop the old one first.
    old = _sc_get(sid)
    if old is not None:
        try:
            old.stop()
        except Exception:  # noqa: BLE001
            pass
    if bad and not headful:
        return bad
    # SHARED capture per tab: a second viewer attaching to a tab that already has a
    # live capture loop reuses it (refcounted by session id) instead of starting a
    # second screencast on the same page.
    if attach_tab_id and not headful:
        with _SIGNIN_LOCK:
            live = next((x for x in _SIGNIN_SESSIONS.values()
                         if x.tab_id == attach_tab_id and not x._stop.is_set()
                         and x.headful == headful), None)
            if live is not None:
                live._refs.add(sid)
                _SIGNIN_SESSIONS[sid] = live
        if live is not None:
            return {"ok": True, "session": sid, "headful": headful, "url": url,
                    "attached": True, "shared": True, "tab_id": live.tab_id}
    sess = _ScreencastSession(sid, url, headful, profile=args.get('profile'),
                              attach_tab_id=attach_tab_id)
    if not sess.start():
        return {"ok": False, "error": "could not open the managed Edge sign-in tab",
                "error_category": "unavailable"}
    with _SIGNIN_LOCK:
        sess._refs.add(sid)
        _SIGNIN_SESSIONS[sid] = sess
    return {"ok": True, "session": sid, "headful": headful, "url": url,
            "attached": bool(attach_tab_id), "shared": False, "tab_id": sess.tab_id}


# A poller that outlives its session (a page left open, an EventSource that reconnects) got a plain application error
# every few seconds - 1755 of them in one morning. The reply is now TERMINAL: state "expired" + terminal:true (callers
# stop polling / do not reconnect) and category not_found (an expected outcome, not a failure).
_NO_SESSION = {"ok": False, "error": "no such sign-in session", "error_category": "not_found",
               "category": "not_found", "state": "expired", "terminal": True}


def signin_shot(args, ctx):
    """Latest frame of a sign-in session: {ok, img(base64 jpeg), url,
    needs_headful}."""
    sess = _sc_get((args.get("session") or "").strip())
    if sess is None:
        return _NO_SESSION
    img, url, needs_headful = sess.shot()
    return {"ok": True, "img": img, "url": url, "needs_headful": needs_headful,
            "w": sess._max_w, "h": 900, "headful": sess.headful,
            "mode": getattr(sess, "mode", "poll"), "text": getattr(sess, "_text", "")}


def signin_input(args, ctx):
    """Relay a click/type/key to the sign-in tab. kind=click{x,y}|type{text}|
    key{key}."""
    sess = _sc_get((args.get("session") or "").strip())
    if sess is None:
        return _NO_SESSION
    kind = (args.get("kind") or "").strip()
    sess._last_input = time.time()
    p = args.get("payload") or {}
    try:
        if kind == "click":
            ok = sess.click(p)
        elif kind == "type":
            ok = sess.type_text(p.get("text", ""))
        elif kind == "key":
            ok = sess.key(p.get("key", ""), p.get("code"), p.get("modifiers", 0))
        elif kind in ("move", "wheel"):
            ok = sess.pointer(kind, p)
        else:
            return {"ok": False, "error": "unknown input kind %r" % kind,
                    "error_category": "application"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    return {"ok": True, "did": kind, "applied": bool(ok)}


def signin_status(args, ctx):
    """Session state: {ok, url, needs_headful, headful, left_login}. `left_login`
    is true once the tab is back on a non-login URL that contains `done_contains`
    (e.g. 'keep.google.com') — the caller's completion signal."""
    sess = _sc_get((args.get("session") or "").strip())
    if sess is None:
        return _NO_SESSION
    _img, url, needs_headful = sess.shot()
    done_sub = (args.get("done_contains") or "").strip().lower()
    low = (url or "").lower()
    left = bool(done_sub and done_sub in low and "accounts.google.com" not in low)
    return {"ok": True, "url": url, "needs_headful": needs_headful,
            "headful": sess.headful, "left_login": left}


def signin_stop(args, ctx):
    """End a sign-in session (close the tab + capture loop)."""
    sid = (args.get("session") or "").strip()
    with _SIGNIN_LOCK:
        sess = _SIGNIN_SESSIONS.pop(sid, None)
        last = True
        if sess is not None:
            sess._refs.discard(sid)
            last = not sess._refs          # other viewers still share this capture loop
    if sess is not None and last:
        try:
            sess.stop()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "stopped": sess is not None}


METHODS = {"snapshot": snapshot, "close": close, "tabs": tabs, "navigate": navigate, "eval": eval_js, "status": status,
           "signin": signin, "device_login": device_login,
           "signin_open": signin_open, "signin_shot": signin_shot,
           "signin_input": signin_input, "signin_status": signin_status,
           "signin_stop": signin_stop,
           "capture_network": capture_network, "get_cookies": get_cookies,
           "stop": stop_browser}
CAPABILITY = "cap:system.cdp"
VARIANT = "sso"           # Windows Edge carries the signed-in corporate session
ATTRS = {"sso": True}
