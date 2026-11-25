"""capability: cap:system.browser.token  (Windows, via the signed-in Edge + CDP).

Endpoint-bound: it extracts an MSAL/OAuth ACCESS TOKEN for a target Microsoft
audience from the user's SIGNED-IN Edge — the browser already holds the corporate
session, so no interactive login is needed. This is the capability behind the
credential app's `requires_browser` tokens (teams/graph/outlook); the app calls
cap:system.browser.token and never touches CDP itself.

How: find (or open) a Teams web tab, read localStorage for msal.* accesstoken
entries, decode each JWT payload, and return the one whose `aud` matches the
requested audience and is unexpired. If a Teams tab isn't open it stages one
(off to the side) and closes it after (unless the caller asked to keep it).

Methods:
  get(audience='graph', tab_id?, stage=True) -> {ok, token, aud, exp, source}
      audience: 'graph' | 'teams' | 'outlook' | a substring to match in aud
  list(tab_id?) -> {ok, tokens:[{aud, exp, preview}]}  (inventory, no secrets leaked beyond preview)

Reuses _cdp_common for the HTTP+WS plumbing. ctx.user maps to the one local
browser (single-user today).
"""
from __future__ import annotations

import base64
import json
import time

from . import _cdp_common as cdp

try:
    from . import _edge_profile as _edge
except Exception:  # noqa: BLE001
    _edge = None

_TEAMS_URL = "https://teams.microsoft.com/v2/"
_FPT_URL = "https://fpt-bnew.seli.gic.ericsson.se/"
# EricAI web app (SPA) — the eristatus dashboard is the real signed-in SPA that
# mints the EricAI API token via MSAL in the browser (the API root `/` is not a
# UI). Staging THIS origin lets us grab the bearer with zero clicks once the
# managed, signed-in Edge can reach EricAI.
_ERICAI_URL = "https://ray.sero.gic.ericsson.se/eristatus/?loaded=true"
# Where to stage (open) a tab to mint a token for an audience that has none open.
_STAGE_URL = {"fpt": _FPT_URL, "ericai": _ERICAI_URL}
# Microsoft web-app origins that keep MSAL tokens in localStorage/sessionStorage.
# We scan any open tab on these origins (the token lives in the origin that
# acquired it — e.g. a Graph token in the signed-in Outlook tab; an FPT token in
# the FPT SPA tab), not just Teams.
_MS_HOSTS = ("teams.microsoft.com", "outlook.cloud.microsoft", "outlook.office.com",
             "m365.cloud.microsoft", "office.com", "microsoft365.com",
             "portal.azure.com", "login.microsoftonline.com",
             "fpt-bnew.seli.gic.ericsson.se", "ray.sero.gic.ericsson.se")
# Audience substrings for the common targets. 'fpt' matches the FPT
# access_as_user token (aud carries the FPT app client-id / 'access_as_user').
# 'ericai' matches the EricAI backend API app (aud = api://<backend-id> or the
# bare backend client-id GUID).
_AUD = {
    "graph": "graph.microsoft",
    "teams": "ic3.teams",
    "outlook": "outlook.office",
    "fpt": "access_as_user",
    "ericai": "b46aa582-485a-4a7d-b30e-552dbd790b16",
}

# JS: collect every unexpired MSAL access token as {aud, exp, secret}. Scans
# BOTH sessionStorage and localStorage, and handles TWO MSAL cache schemas:
#   (legacy) a key containing 'accesstoken' whose value has .secret/.accessToken
#   (v3/v4)  entries keyed 'msal.<n>|<clientid>...' whose JSON value has
#            credentialType=="AccessToken" + .secret (the modern *.cloud.microsoft
#            apps — m365/outlook/onenote — use THIS; the key carries no audience,
#            so the legacy 'accesstoken'-key filter missed them entirely).
# For each, the audience needle is matched against the JWT's `aud` claim + the
# entry's `target` (scopes) + the key, so 'graph.microsoft' still resolves.
_JS_COLLECT = (
    "(function(){var stores=[sessionStorage,localStorage],out=[],now=Math.floor(Date.now()/1000);"
    "function add(t,hint){if(!t)return;try{"
    "var p=JSON.parse(atob(t.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));"
    "if(p.exp&&p.exp<now)return;"
    "out.push({aud:(p.aud||'')+' '+(hint||''),exp:p.exp||0,secret:t});"
    "}catch(e){}}"
    "for(var s=0;s<stores.length;s++){var st=stores[s],ks=Object.keys(st);"
    "for(var i=0;i<ks.length;i++){var k=ks[i],kl=k.toLowerCase();"
    "var raw=st.getItem(k);if(!raw||raw.indexOf('eyJ')<0)continue;"
    "var v=null;try{v=JSON.parse(raw);}catch(e){}"
    "if(kl.indexOf('accesstoken')>=0&&v){add(v.secret||v.accessToken,(v.target||'')+' '+k);continue;}"
    "if(v&&v.credentialType==='AccessToken'&&v.secret){add(v.secret,(v.target||'')+' '+k);continue;}"
    "if(v&&(v.secret||v.accessToken)&&(v.credentialType==='AccessToken'||v.tokenType)){add(v.secret||v.accessToken,(v.target||'')+' '+k);}"
    "}}return JSON.stringify(out);})()"
)


def _decode_exp(tok):
    try:
        p = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
        return int(p.get("exp", 0)), str(p.get("aud", ""))
    except Exception:  # noqa: BLE001
        return 0, ""


# --- per-audience token cache (keyed by the requested audience) -------------
# get() otherwise re-scans every MS tab's localStorage on EVERY call. An MSAL
# access token is valid for ~60-90 min and already carries its own `exp`; cache
# the last good token per audience and reuse it until exp - skew, so repeated
# graph/fpt/outlook asks don't each drive CDP. Cleared implicitly by expiry.
_TOKEN_CACHE = {}          # cache-key -> {"secret", "exp", "source"}
_EXP_SKEW_S = 120          # treat a token as expired this early


def _ckey(audience, require_scope=""):
    """Cache key: audience, plus the required scope when one is asked for. A
    scope-required ask never serves a cached narrower token (different key), but
    _cache_get's superset fallback lets a cached scope-BEARING token serve a
    plain read for the same audience (#8)."""
    a = (audience or "").lower()
    s = (require_scope or "").strip().lower()
    return a + ("|" + s if s else "")


def _fresh(e):
    """True when a cache entry exists and is still inside its lifetime."""
    if not e:
        return False
    return not (e["exp"] and e["exp"] - int(time.time()) <= _EXP_SKEW_S)


def _cache_get(ckey):
    e = _TOKEN_CACHE.get(ckey)
    if not e:
        # #8 SUPERSET SERVE: a plain-audience ask (no '|scope' in ckey) can be
        # satisfied by ANY still-valid cached token for the SAME audience,
        # including a write-scoped one cached under 'audience|scope' — a superset
        # token is valid for reads, so this avoids minting/scanning twice for the
        # same audience. (A scope-REQUIRING ask keeps its exact key: it must get
        # a token proven to carry the scope.)
        if "|" not in ckey:
            aud = ckey + "|"
            for k, ent in list(_TOKEN_CACHE.items()):
                if k == ckey or k.startswith(aud):
                    if _fresh(ent):
                        return ent
                    _TOKEN_CACHE.pop(k, None)
        return None
    if not _fresh(e):
        _TOKEN_CACHE.pop(ckey, None)
        return None
    return e


def _cache_put(ckey, secret, source=""):
    exp, _aud = _decode_exp(secret)
    _TOKEN_CACHE[ckey] = {"secret": secret, "exp": exp, "source": source}


def _collect_tokens(target):
    r = cdp.eval_on_target_pooled(target, _JS_COLLECT, timeout=15)
    if not r.get("ok"):
        return None, r
    try:
        toks = json.loads(r.get("value") or "[]")
    except (TypeError, json.JSONDecodeError):
        toks = []
    return toks, None


def _ms_tabs():
    """Open page tabs on a known Microsoft origin (most-likely token holders)."""
    out = []
    for t in cdp.page_tabs():
        url = (t.get("url") or "").lower()
        if any(h in url for h in _MS_HOSTS) and t.get("webSocketDebuggerUrl"):
            out.append(t)
    return out


def _scan_for_audience(needle, require_scope=None):
    """Scan every open MS tab's localStorage for an unexpired token matching the
    audience. When ``require_scope`` is given, this is authoritative ACROSS tabs:
    a scope-bearing token in a LATER tab beats a scope-missing aud-match in an
    earlier one (the write-scoped outlook token may live in a different tab than
    the first-scanned one — #24). Returns (secret, source_url) or (None, None).

    Two passes when a scope is required: first look for a token that actually
    carries the scope in ANY tab; only if none does, fall back to the first
    aud-match (preserving read-only behaviour). Without a required scope it
    short-circuits on the first aud-match (unchanged, one scan)."""
    want = (require_scope or "").lower()
    first_match = None          # (secret, url) fallback when no tab has the scope
    for t in _ms_tabs():
        toks, err = _collect_tokens(t)
        if err:
            continue
        if not want:
            sec = _pick(toks, needle)
            if sec:
                return sec, t.get("url", "")
            continue
        # scope required: prefer a scope-bearing token in THIS tab, but keep
        # scanning the rest if this tab only has a narrower aud-match.
        sec = _pick(toks, needle, require_scope=want)
        if sec and want in _decode_scp(sec):
            return sec, t.get("url", "")
        if first_match is None and sec:
            first_match = (sec, t.get("url", ""))
    if first_match:
        return first_match
    return None, None


def _nudge_and_rescan(needle, settle_s=12, require_scope=None):
    """When a scan found only EXPIRED tokens, reload an already-open MS tab so its
    MSAL silently re-acquires, then rescan once. Reloads the tab IN PLACE (does
    not open a new Teams/session) → non-disruptive. Returns (secret, url) or
    (None, None). Picks the tab most likely to hold the audience: prefer an
    Outlook/Office tab for graph/outlook, any MS tab otherwise."""
    tabs = _ms_tabs()
    if not tabs:
        return None, None
    # Prefer a non-Teams MS tab (Outlook/Office) so we don't reload a Teams tab
    # (Teams web reload is heavier + the sync path is sensitive); fall back to any.
    pref = [t for t in tabs if "teams." not in (t.get("url") or "").lower()]
    target = (pref or tabs)[0]
    try:
        # Reload via CDP Page.reload on the target's own WS (re-trigger MSAL).
        cdp.eval_on_target_pooled(target, "location.reload()", timeout=8)
    except Exception:  # noqa: BLE001
        return None, None
    time.sleep(settle_s)
    # Rescan (the reloaded tab's target id/url is stable across a reload).
    return _scan_for_audience(needle, require_scope=require_scope)


def _ensure_endpoint():
    """Make sure a debuggable managed Edge is reachable, launching the persistent
    signed-in profile if nothing is up, then point _cdp_common at it. Returns an
    error dict or None.

    Token scanning only reads localStorage/sessionStorage, which works IDENTICALLY
    headed or headless — so this must NOT force a particular mode. Forcing
    headless here used to make ensure_mode() KILL + RELAUNCH an already-running
    HEADED Edge (the mode the Teams chat-sync / browser.fetch path latches),
    tearing the shared browser down mid-session every time a token was minted —
    which destabilized any live Teams/Office web session in that profile (the
    reported "Teams message not sent" thrash). Fix: REUSE whatever Edge is
    already running in whatever mode; only launch (headless by default) when
    nothing is up at all. Never flip the mode of a live instance."""
    if _edge is None:
        return cdp.ensure_available()
    # Reuse a live managed Edge AS-IS (any mode) — never relaunch it for a token read.
    dbg = None
    try:
        mode = _edge.current_mode()        # None when nothing is running
        if mode is not None:
            import os as _os
            p = int(open(_edge.PORT_FILE).read().strip())
            dbg = "http://127.0.0.1:%d" % p
    except Exception:  # noqa: BLE001
        dbg = None
    if not dbg:
        # Nothing running → launch (respecting the headed latch inside ensure_running).
        dbg = _edge.ensure_running(headless=True)
    if not dbg:
        return cdp.ensure_available()
    cdp.configure(dbg)
    return cdp.ensure_available()


def get(args, ctx):
    audience = (args.get("audience") or "graph").strip()
    needle = _AUD.get(audience.lower(), audience)
    # #24: a caller can require a specific scope (e.g. 'Calendars.ReadWrite' for a
    # booking WRITE) — MSAL may hold several tokens for one audience with
    # different scope sets, so pick one that actually carries it. The cache key
    # includes the required scope so a scope-required ask never serves a cached
    # narrower token.
    req_scope = (args.get("require_scope") or "").strip()
    ckey = _ckey(audience, req_scope)
    # 0) serve a still-valid cached token for this audience (skip the CDP scan).
    if not args.get("no_cache"):
        hit = _cache_get(ckey)
        if hit:
            return _ok(hit["secret"], hit.get("source", ""))
    bad = _ensure_endpoint()
    if bad:
        return bad
    stage = args.get("stage", True)
    opened = None
    try:
        # 1) explicit tab_id, if given.
        tab_id = args.get("tab_id")
        if tab_id:
            tgt = cdp.target_by_id(tab_id)
            if tgt:
                toks, err = _collect_tokens(tgt)
                if not err:
                    sec = _pick(toks, needle, require_scope=req_scope)
                    if sec:
                        _cache_put(ckey, sec)
                        return _ok(sec)
        # 2) scan every already-open Microsoft tab (the common case: the token
        #    lives in the signed-in Outlook/Teams/Office tab).
        sec, src = _scan_for_audience(needle, require_scope=req_scope)
        if sec:
            _cache_put(ckey, sec, src)
            return _ok(sec, src)
        # 2.5) NUDGE-ON-EXPIRY. An idle MS tab lets its MSAL access tokens expire
        #      (no silent renewal fires without activity), so a scan finds only
        #      EXPIRED tokens → nothing. Before staging a new tab, RELOAD an
        #      already-open MS tab in place (non-disruptive — it's the existing
        #      Outlook/Office tab, not a new Teams session) so MSAL re-acquires,
        #      then rescan once. This self-heals the common "tokens went stale
        #      after an hour idle" case (the getSchedule organizer path).
        if not args.get("no_nudge"):
            sec, src = _nudge_and_rescan(needle, require_scope=req_scope)
            if sec:
                _cache_put(ckey, sec, src)
                return _ok(sec, src)
        # 3) stage a tab for this audience, let MSAL populate, and rescan. Only
        #    if the caller allows staging. The staged URL is audience-aware
        #    (FPT → the FPT SPA; graph/teams/outlook → Teams, which mints MS
        #    tokens and — via the calendar nudge — graph/outlook scopes).
        #    NOTE: staging opens a tab in the DEDICATED managed Edge profile
        #    (separate from the user's own Edge/desktop Teams session), and no
        #    longer flips the shared Edge's headed/headless mode (see
        #    _ensure_endpoint) — so it doesn't kill a live session. Teams supports
        #    concurrent sessions, so a staged web login is not disruptive by itself.
        if stage:
            stage_url = _STAGE_URL.get(audience.lower(), _TEAMS_URL)
            o = cdp.open_tab(stage_url)
            opened = o.get("id")
            cdp.wait_for_ws(opened, timeout=10)
            time.sleep(6)
            tgt = cdp.target_by_id(opened)
            if tgt:
                toks, err = _collect_tokens(tgt)
                sec = _pick(toks, needle, require_scope=req_scope) if not err else None
                if not sec and stage_url == _TEAMS_URL:
                    cdp.eval_on_target(
                        tgt, "window.location.href='https://teams.microsoft.com/v2/calendar'")
                    time.sleep(6)
                    toks, err = _collect_tokens(cdp.target_by_id(opened) or tgt)
                    sec = _pick(toks, needle, require_scope=req_scope) if not err else None
                if sec:
                    _cache_put(ckey, sec, _TEAMS_URL)
                    return _ok(sec, _TEAMS_URL)
        return {"ok": False,
                "error": f"no unexpired token for audience '{audience}' in the signed-in browser "
                         "(sign in to a Microsoft web app, or open the right tab)",
                "error_category": "unavailable"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    finally:
        if opened and not args.get("keep_tab"):
            cdp.close_tab(opened)


def _ok(secret, source=""):
    exp, aud = _decode_exp(secret)
    return {"ok": True, "token": secret, "aud": aud, "exp": exp,
            "expires_in": max(0, exp - int(time.time())),
            "source": "edge-localstorage", "source_url": source}


def _decode_scp(tok):
    """The JWT's space-separated scopes (scp claim), lowercased. '' on failure."""
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        p = json.loads(base64.urlsafe_b64decode(seg))
        return str(p.get("scp") or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _pick(toks, needle, require_scope=None):
    """Pick an aud-matching token. When ``require_scope`` is given, PREFER a
    candidate whose JWT scp contains it (MSAL can hold several tokens for the
    same audience with different scope sets — e.g. an outlook.office token with
    Calendars.ReadWrite vs. a narrower one; the first-match-wins behaviour
    returned the wrong one for the room-booking WRITE, #24). Falls back to the
    first aud-match when none carries the scope (so read-only asks are unchanged)."""
    want = (require_scope or "").lower()
    matches = [t for t in (toks or []) if needle.lower() in (t.get("aud") or "").lower()]
    if want:
        for t in matches:
            if want in _decode_scp(t.get("secret") or ""):
                return t.get("secret")
    return matches[0].get("secret") if matches else None


def list_tokens(args, ctx):
    bad = _ensure_endpoint()
    if bad:
        return bad
    tab_id = args.get("tab_id")
    targets = [cdp.target_by_id(tab_id)] if tab_id else _ms_tabs()
    targets = [t for t in targets if t and t.get("webSocketDebuggerUrl")]
    if not targets:
        return {"ok": False, "error": "no signed-in Microsoft tab to inventory "
                "(open a Microsoft web app or pass a tab_id)",
                "error_category": "unavailable"}
    seen, out = set(), []
    for target in targets:
        toks, err = _collect_tokens(target)
        if err:
            continue
        for t in toks or []:
            sec = t.get("secret") or ""
            aud = t.get("aud", "")
            scp = _decode_scp(sec)
            # #24: dedupe by (aud, scopes) rather than aud alone — MSAL can hold
            # several tokens for ONE audience with DIFFERENT scope sets (the
            # root of the booking 403), so collapsing on aud would hide the
            # narrower-vs-write-scoped split this inventory exists to reveal.
            key = (aud, scp)
            if key in seen:
                continue
            seen.add(key)
            out.append({"aud": aud, "exp": t.get("exp", 0),
                        "expires_in": max(0, int(t.get("exp", 0)) - int(time.time())),
                        "preview": (sec[:16] + "…") if sec else "",
                        # #24 DIAGNOSTIC: surface the token's scopes (scp) so the
                        # op edge can see WHICH outlook token carries
                        # Calendars.ReadWrite without a daemon-internal trace.
                        # Non-secret (scope names only).
                        "scopes": scp,
                        "source_url": target.get("url", "")})
    return {"ok": True, "count": len(out), "tokens": out}


METHODS = {"get": get, "list": list_tokens}
CAPABILITY = "cap:system.browser.token"
VARIANT = "sso"
ATTRS = {"sso": True}
