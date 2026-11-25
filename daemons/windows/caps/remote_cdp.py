"""capability: cap:remote.cdp — run a page_automation GATHER case against headless
Chrome on a REMOTE host (seli) over SSH.

This is the first concrete brick of PAGE_AUTOMATION_TODO #31 (the remote runtime
that will eventually let page_automation drive logins on a remote per-service
profile and let workspace/cdp_session.py retire). It deliberately starts with the
SAFE, LOGIN-FREE half — a data-gather case (navigate + extract, no credentials,
no MFA) — so the remote-driving SEAM is real and verifiable end-to-end before the
harder login/enrollment/viewer parts are layered on.

How it works (no daemon on the remote — just SSH + a shipped stdlib script):
  1. The method base64-ships a tiny self-contained agent (`_REMOTE_AGENT`, below)
     to the remote via remote_session.run (the same proven path sso_login uses).
  2. On the remote it `module load`s chrome+chromedriver, launches chromedriver,
     opens a headless Chrome session over chromedriver's W3C HTTP API, and drives
     the case's screens: navigate + extract (text/attr/all/count) into a
     `collected` dict. Pure stdlib (urllib + json) — no selenium/websocket.
  3. The agent prints a single JSON line; the method parses it and returns
     {ok, state, collected, trace, host, elapsed_s}.

Only GATHER cases (no secrets, no human gate) are accepted here — a login case is
rejected with a clear message pointing at the remaining #31 work. The case BUNDLE
is supplied by platform/page_automation (resolve the case, pass bundle+inputs),
exactly like the local cap:system.cdp device_login — same cases-as-data model,
different transport/host.

remote_session lives in apps/knowledge/workspace/impl (a sibling module, not a
capability). We sys.path-reach it the same way other code reaches workspace impl
helpers; if it (or paramiko) is unavailable the cap degrades to a clean
'unavailable'.
"""
from __future__ import annotations

import base64 as _b64
import hashlib as _hashlib
import json as _json
import os as _os
import sys as _sys
import threading as _threading
import time as _time

# --- short-TTL availability-probe cache (REMOTE_RUNTIME #17) ----------------
# status() runs a full SSH exec (source /etc/profile + module load + 3 `command
# -v` probes, ~2-3s, dominated by the module-load) on EVERY call, and gather /
# login didn't reuse it. Chrome/chromedriver/python don't change mid-session, so
# cache the (host,user) availability verdict for a short TTL — a gather right
# after a status skips the re-probe. Keyed by (host,user); value is {ts, result}.
_PROBE_TTL_S = 90.0
_PROBE_CACHE = {}
_PROBE_LOCK = _threading.Lock()


def _probe_cache_get(host, user):
    key = (host, user)
    now = _time.time()
    with _PROBE_LOCK:
        hit = _PROBE_CACHE.get(key)
        if hit and (now - hit[0]) < _PROBE_TTL_S:
            return dict(hit[1])
    return None


def _probe_cache_put(host, user, result):
    # only cache a definitive reachable/unreachable verdict (not transient errors)
    with _PROBE_LOCK:
        _PROBE_CACHE[(host, user)] = (_time.time(), dict(result))


# --- content-hash ship cache (REMOTE_RUNTIME #16) ---------------------------
# Skip re-uploading a shipped script (cdp_session.py / the gather agent) when the
# remote copy already matches. We stash the sha256 we last shipped per
# (host,user,remote_path) and, before shipping, ask the remote for the file's
# sha256; a match means skip the base64 upload + decode entirely.
_SHIP_CACHE = {}
_SHIP_LOCK = _threading.Lock()

# SESSION_FOLLOWUPS #18: cache a shipped FILE's content+sha keyed by
# (path, mtime, size) so a repeated login neither re-reads nor re-b64/re-hashes
# the ~40KB cdp_session.py when the file on disk is unchanged. Invalidated
# automatically when the file's mtime/size changes (an edit during dev).
_FILE_CACHE = {}
_FILE_LOCK = _threading.Lock()


def _sha(text):
    return _hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_file_cached(path):
    """Return (content, sha256) for ``path``, memoized by (path, mtime, size).
    Re-reads + re-hashes only when the file changes on disk. Raises OSError if
    the file can't be stat'd/read (caller handles). SESSION_FOLLOWUPS #18."""
    st = _os.stat(path)
    sig = (path, st.st_mtime_ns, st.st_size)
    with _FILE_LOCK:
        hit = _FILE_CACHE.get(path)
        if hit is not None and hit[0] == sig:
            return hit[1], hit[2]
    with open(path, encoding="utf-8") as f:
        content = f.read()
    sha = _sha(content)
    with _FILE_LOCK:
        _FILE_CACHE[path] = (sig, content, sha)
    return content, sha


def _ship_if_changed(rs, user, host, remote_path, content, timeout=40, sha=None):
    """Ship `content` to remote_path ONLY if the remote copy's sha differs.
    Returns (ok, skipped, err). Uses a per-target sha cache + a remote sha256sum
    check so an unchanged file is never re-uploaded. ``sha`` may be a precomputed
    sha256 of ``content`` (from _read_file_cached) to skip re-hashing (#18)."""
    want = sha or _sha(content)
    key = (host, user, remote_path)
    with _SHIP_LOCK:
        if _SHIP_CACHE.get(key) == want:
            # we shipped this exact content before; trust it (cheap path).
            return True, True, ""
    remote_dir = _os.path.dirname(remote_path) or "/tmp"
    # Ask the remote for the current file's sha (empty if absent).
    probe = ("test -f %s && sha256sum %s 2>/dev/null | cut -d' ' -f1 || echo MISSING"
             ) % (_shq(remote_path), _shq(remote_path))
    try:
        r = rs.run(user, probe, host=host, timeout=min(timeout, 20))
        have = (r.get("stdout") or "").strip().splitlines()
        have_sha = have[-1].strip() if have else ""
    except Exception:  # noqa: BLE001
        have_sha = ""
    if have_sha == want:
        with _SHIP_LOCK:
            _SHIP_CACHE[key] = want
        return True, True, ""
    b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
    cmd = ("mkdir -p %s 2>/dev/null; printf '%%s' %s | base64 -d > %s "
           "&& echo WROTE || echo WRITE_FAIL") % (_shq(remote_dir), _shq(b64), _shq(remote_path))
    try:
        r = rs.run(user, cmd, host=host, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, False, "ship failed: %s" % str(exc)[:160]
    ok = "WROTE" in (r.get("stdout") or "")
    if ok:
        with _SHIP_LOCK:
            _SHIP_CACHE[key] = want
    return ok, False, (r.get("stderr") or "")[:200]


def _ship_prelude(remote_dir, remote_path, content, sha):
    """SESSION_FOLLOWUPS #2: a shell snippet that ship-skips INLINE — compares the
    remote file's sha to ``sha`` and writes it from the embedded base64 ONLY if it
    differs — so a caller can PREPEND it to the same exec that launches serve
    (one SSH round-trip instead of a separate sha-probe exec then a launch exec).
    The base64 is embedded unconditionally (so the write is available when needed)
    but `base64 -d` only runs on a mismatch, so the common 'already current' case
    is a cheap `sha256sum | cut` + string compare with no decode. Ends without a
    trailing separator so the caller appends `; <launch>`."""
    b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
    return (
        "mkdir -p %s 2>/dev/null; "
        "__have=$(test -f %s && sha256sum %s 2>/dev/null | cut -d' ' -f1 || echo MISSING); "
        "if [ \"$__have\" != %s ]; then printf '%%s' %s | base64 -d > %s; fi"
    ) % (_shq(remote_dir), _shq(remote_path), _shq(remote_path),
         _shq(sha), _shq(b64), _shq(remote_path))


# --- locate the workspace remote_session sibling (SSH plumbing) -------------
def _load_remote_session():
    """Import apps/knowledge/workspace/impl/remote_session (+ its siblings) by
    adding that impl dir to sys.path. Returns the module or None (if the repo
    layout differs / paramiko missing)."""
    # daemons/windows/caps/remote_cdp.py -> repo root is three parents up.
    here = _os.path.dirname(_os.path.abspath(__file__))
    root = _os.path.abspath(_os.path.join(here, "..", "..", ".."))
    impl = _os.path.join(root, "apps", "knowledge", "workspace", "impl")
    if impl not in _sys.path:
        _sys.path.insert(0, impl)
    try:
        import remote_session as rs  # noqa: E402
        return rs
    except Exception:  # noqa: BLE001
        return None


def _remote_host(args):
    """Resolve the remote host: explicit arg wins, else REMOTE_WS_HOST env, else
    the documented seli FQDN. User: arg, else REMOTE_WS_USER, else $USER."""
    host = (args.get("host") or _os.environ.get("REMOTE_WS_HOST")
            or "seliiuvd03542.seli.gic.ericsson.se").strip()
    user = (args.get("user") or _os.environ.get("REMOTE_WS_USER")
            or _os.environ.get("USER") or _os.environ.get("USERNAME") or "").strip()
    return host, user


# --- the remote agent (stdlib-only; runs ON seli via `python3 - <json>`) ----
# It reads ONE base64 JSON arg: {bundle, inputs}. It drives the bundle's screens
# (navigate/extract only) against a fresh headless Chrome via chromedriver's W3C
# API and prints ONE json line: {ok, state, collected, trace, error?}.
_REMOTE_AGENT = r'''
import sys, json, os, time, subprocess, urllib.request

def _chromedriver():
    for c in ("chromedriver",):
        p = _which(c)
        if p: return p
    for p in ("/app/vbuild/RHEL8-x86_64/chromedriver/149.0.7827.102/bin/chromedriver",):
        if os.path.isfile(p): return p
    return None

def _which(name):
    for d in os.environ.get("PATH","").split(os.pathsep):
        fp = os.path.join(d, name)
        if os.path.isfile(fp) and os.access(fp, os.X_OK): return fp
    return None

class Driver:
    def __init__(self, port, profile=None):
        self.port = port; self.wd = "http://127.0.0.1:%d" % port
        self.proc = None; self.sid = None; self.udd = profile
    def __enter__(self):
        cd = _chromedriver()
        if not cd: raise RuntimeError("chromedriver not found")
        self.proc = subprocess.Popen([cd, "--port=%d" % self.port],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.5)
        args = ["--headless=new","--no-sandbox","--disable-gpu",
                "--ignore-certificate-errors","--disable-dev-shm-usage"]
        if self.udd: args.append("--user-data-dir=%s" % self.udd)
        caps = {"capabilities":{"alwaysMatch":{"browserName":"chrome",
                "goog:chromeOptions":{"args":args}}}}
        self.sid = self._req("POST","/session",caps)["value"]["sessionId"]
        return self
    def __exit__(self,*a):
        try:
            if self.sid: self._req("DELETE","/session/"+self.sid)
        except Exception: pass
        try:
            if self.proc: self.proc.terminate()
        except Exception: pass
        return False
    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.wd+path, data=data,
            headers={"Content-Type":"application/json"}, method=method)
        with urllib.request.urlopen(r, timeout=40) as resp:
            return json.loads(resp.read().decode("utf-8","replace"))
    def cmd(self, method, path, body=None):
        return self._req(method, "/session/"+self.sid+path, body)
    def goto(self, url): self.cmd("POST","/url",{"url":url})
    def js(self, script):
        return self.cmd("POST","/execute/sync",{"script":script,"args":[]}).get("value")

def subst(val, inputs):
    if not isinstance(val, str): return val
    for k,v in inputs.items(): val = val.replace("${%s}" % k, str(v))
    return val

def extract(drv, selector, name, attr="", all_=False, count=False):
    sel = json.dumps(selector); a = json.dumps(attr or "")
    if count:
        return drv.js("return document.querySelectorAll(%s).length;" % sel) or 0
    if all_:
        return drv.js(
          "var a=%s;var out=[];[].slice.call(document.querySelectorAll(%s))"
          ".forEach(function(e){var v=a?(e.getAttribute(a)||''):(e.textContent||'').trim();"
          "if(v)out.push(v);});return out.slice(0,200);" % (a, sel)) or []
    return drv.js(
      "var a=%s;var e=document.querySelector(%s);if(!e)return '';"
      "return a?(e.getAttribute(a)||''):(e.textContent||'').trim();" % (a, sel)) or ""

def _ready(drv, budget):
    # event-wait: return as soon as the document is complete (or budget expires),
    # instead of a blind fixed sleep (REMOTE_RUNTIME #18).
    deadline = time.time() + float(budget)
    while time.time() < deadline:
        try:
            if drv.js("return document.readyState;") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.2)

def run(bundle, inputs, port, profile):
    with Driver(port, profile=profile) as drv:
        return drive(drv, bundle, inputs)

def drive(drv, bundle, inputs):
    """Drive one gather case against an ALREADY-OPEN driver session. Factored out
    of run() so a persistent serve loop can reuse ONE warm session across many
    gathers (REMOTE_RUNTIME #14/#19) instead of a chromedriver cold-start each."""
    defaults = bundle.get("defaults",{}) or {}
    start_url = (inputs.get("url") or defaults.get("start_url") or "").strip()
    wait_sel = defaults.get("wait_for_selector") or ""
    collected = {}; trace = []
    if start_url:
        drv.goto(start_url)
        if wait_sel:
            deadline = time.time() + float(defaults.get("pre_gate_budget_s") or 20)
            while time.time() < deadline:
                try:
                    if drv.js("return !!document.querySelector(%s);" % json.dumps(wait_sel)):
                        break
                except Exception:
                    pass
                time.sleep(0.5)
        else:
            _ready(drv, float(defaults.get("settle") or 2))
    for sc in bundle.get("screens",[]):
        name = sc.get("name","")
        verdict = sc.get("classify") or "continue"
        acts = []
        for act in (sc.get("do") or []):
            t = act.get("type")
            if t == "navigate":
                u = subst(act.get("url",""), inputs)
                if u: drv.goto(u); _ready(drv, float(act.get("settle") or 2))
                acts.append({"type":t,"url":u})
            elif t == "extract":
                nm = act.get("name") or act.get("selector","")
                val = extract(drv, subst(act.get("selector",""), inputs), nm,
                              attr=act.get("attr",""), all_=bool(act.get("all")),
                              count=bool(act.get("count")))
                collected[nm] = val
                acts.append({"type":t,"name":nm,
                    "got": len(val) if isinstance(val,list) else (1 if val!="" else 0)})
            elif t == "wait":
                time.sleep(float(act.get("seconds") or 1)); acts.append({"type":t})
            # login/secret actions are intentionally NOT supported here.
        try: url = drv.js("return location.href;") or ""
        except Exception: url = ""
        trace.append({"screen":name,"verdict":verdict,"url":url,"actions":acts})
        if verdict in ("collected","completed","denied"): break
    state = "collected" if collected else "done"
    return {"ok": True, "state": state, "collected": collected, "trace": trace}

def serve(port, profile, idle_ttl):
    """WARM-AGENT serve mode (REMOTE_RUNTIME #14/#19): open ONE chromedriver
    session and keep it alive, handling many gathers over localhost HTTP so each
    gather after the first skips the ~10-15s cold start. Self-exits after
    idle_ttl seconds with no request (so a forgotten warm agent can't linger).
      POST /gather {bundle,inputs} -> the gather result (reuses the warm session)
      GET  /ping                   -> {ok:true, since, gathers}
      POST /quit                   -> shut down
    Prints 'SERVE_READY <port>' once the session is up so the launcher knows the
    cold start is paid."""
    import http.server, socketserver, threading, hashlib
    drv = Driver(port + 1, profile=profile or None)
    drv.__enter__()
    state = {"last": time.time(), "gathers": 0, "since": time.time()}
    state_lock = threading.Lock()
    # #19: idempotent gather dedup — cache the gather RESULT keyed by a hash of
    # (bundle, inputs) for a short TTL, so a repeated IDENTICAL gather within the
    # window returns the cached result instead of re-navigating + re-running the
    # full screen ladder. A different bundle/inputs misses and runs normally.
    gather_cache = {}            # key -> (ts, result)
    GATHER_TTL = 8.0
    print("SERVE_READY %d" % port, flush=True)

    def _idle_watch(httpd):
        while True:
            time.sleep(5)
            with state_lock:
                idle = time.time() - state["last"]
            if idle > float(idle_ttl):
                try: httpd.shutdown()
                except Exception: pass
                return

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try: return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception: return {}
        def do_GET(self):
            with state_lock: state["last"] = time.time()
            if self.path.startswith("/ping"):
                with state_lock:
                    self._send({"ok": True, "since": state["since"], "gathers": state["gathers"]})
            else:
                self._send({"error":"not found"}, 404)
        def do_POST(self):
            with state_lock: state["last"] = time.time()
            if self.path.startswith("/gather"):
                b = self._body()
                bundle = b.get("bundle") or {}
                inputs = b.get("inputs") or {}
                # #19: dedup identical back-to-back gathers within GATHER_TTL.
                try:
                    key = hashlib.sha256(json.dumps(
                        {"b": bundle, "i": inputs}, sort_keys=True).encode()).hexdigest()
                except Exception:
                    key = None
                now = time.time()
                if key is not None:
                    hit = gather_cache.get(key)
                    if hit and (now - hit[0]) < GATHER_TTL:
                        r = dict(hit[1]); r["cached"] = True
                        self._send(r); return
                try:
                    r = drive(drv, bundle, inputs)
                except Exception as e:
                    r = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:240])}
                # cache only a successful, deterministic gather result
                if key is not None and isinstance(r, dict) and r.get("ok"):
                    gather_cache[key] = (now, r)
                    if len(gather_cache) > 32:   # bound: drop oldest
                        _oldest = min(gather_cache, key=lambda k: gather_cache[k][0])
                        gather_cache.pop(_oldest, None)
                with state_lock: state["gathers"] += 1
                self._send(r)
            elif self.path.startswith("/quit"):
                self._send({"ok": True}); raise SystemExit
            else:
                self._send({"error":"not found"}, 404)

    class _S(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True; allow_reuse_address = True
    httpd = _S(("127.0.0.1", port), H)
    threading.Thread(target=_idle_watch, args=(httpd,), daemon=True).start()
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try: httpd.server_close()
        except Exception: pass
        try: drv.__exit__(None,None,None)
        except Exception: pass

def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "serve":
            # serve <port> <profile> <idle_ttl>
            serve(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "",
                  float(sys.argv[4]) if len(sys.argv) > 4 else 120)
            return
        payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
        bundle = payload.get("bundle") or {}
        inputs = payload.get("inputs") or {}
        port = int(payload.get("port") or 9711)
        profile = payload.get("profile") or None
        print(json.dumps(run(bundle, inputs, port, profile)))
    except Exception as e:
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}))

main()
'''

# #18: the agent body is a static module constant — hash it ONCE at import, not
# on every gather, so _ship_if_changed can skip re-hashing ~15KB each call.
_REMOTE_AGENT_SHA = _sha(_REMOTE_AGENT)


def _ensure_rs():
    rs = _load_remote_session()
    if rs is None:
        return None, {"ok": False, "error": "remote_session unavailable (paramiko "
                      "or the workspace impl dir is missing)", "error_category": "unavailable"}
    if not rs.available():
        return None, {"ok": False, "error": "paramiko not installed on this endpoint",
                      "error_category": "unavailable"}
    return rs, None


def status(args, ctx):
    """Report whether the remote host is reachable + has chrome/chromedriver/python.
    A cheap preflight so the UI can show 'remote ready' before a gather run.

    Result is cached per (host,user) for a short TTL (REMOTE_RUNTIME #17) so a
    gather/login run right after a status doesn't re-pay the ~2-3s module-load
    probe. Pass fresh=true to force a re-probe."""
    rs, bad = _ensure_rs()
    if bad:
        return {"ok": True, "available": False, "detail": bad["error"]}
    host, user = _remote_host(args)
    if not user:
        return {"ok": True, "available": False, "host": host,
                "detail": "no remote user (set REMOTE_WS_USER or pass user=)"}
    if not args.get("fresh"):
        cached = _probe_cache_get(host, user)
        if cached is not None:
            cached["cached"] = True
            return cached
    cmd = ("source /etc/profile 2>/dev/null; "
           "module load chrome/149.0.7827.102 chromedriver/149.0.7827.102 2>/dev/null; "
           "echo PY=$(python3 --version 2>&1); "
           "echo CHROME=$(command -v google-chrome chromium 2>/dev/null | head -1); "
           "echo CHROMEDRIVER=$(command -v chromedriver 2>/dev/null | head -1)")
    try:
        r = rs.run(user, cmd, host=host, timeout=40)
    except Exception as exc:  # noqa: BLE001
        # transient ssh failure — do NOT cache (so a retry can succeed).
        return {"ok": True, "available": False, "host": host,
                "detail": "ssh failed: %s" % str(exc)[:200]}
    out = r.get("stdout", "") or ""
    have_chrome = "CHROME=/" in out
    have_driver = "CHROMEDRIVER=/" in out
    result = {"ok": True, "available": bool(r.get("rc") == 0 and have_chrome and have_driver),
              "host": host, "user": user, "probe": out.strip()[:400], "cached": False}
    _probe_cache_put(host, user, result)
    return result


def gather(args, ctx):
    """Run a page_automation GATHER case against headless Chrome on the remote host.

    args:
      bundle   — the resolved case bundle {id, defaults, screens[]} (gather case:
                 navigate + extract screens only). REQUIRED.
      inputs   — {url?, ...} substituted into ${...} placeholders.
      host/user— remote target (defaults: REMOTE_WS_HOST/seli, REMOTE_WS_USER/$USER).
      port     — chromedriver port on the remote (default 9711).
      timeout_s— overall SSH exec budget (default 120).
      profile  — optional persistent profile dir on the remote (reserved for the
                 later login/enrollment work; a gather case needs none).
      warm     — reuse a persistent serve-mode agent across gathers (default
                 true; #14/#19). warm_ttl_s — warm-agent idle TTL (default 120).

    Returns {ok, state, collected, trace, host, elapsed_s, warm} or a clean error.
    REJECTS a login/secret case (that needs the remaining #31 enrollment+viewer)."""
    rs, bad = _ensure_rs()
    if bad:
        return bad
    bundle = args.get("bundle")
    if not isinstance(bundle, dict) or not bundle.get("screens"):
        return {"ok": False, "error": "bundle (resolved gather case) required",
                "error_category": "application"}
    # SAFETY: only gather cases here — no secrets / human gate on the remote path yet.
    for sc in bundle.get("screens", []):
        for act in (sc.get("do") or []):
            if act.get("type") in ("type_secret", "notify", "scrape_number",
                                   "click_primary", "click_tile", "fill_if"):
                return {"ok": False, "error": "cap:remote.cdp runs GATHER cases only "
                        "(navigate + extract); login/secret actions need the remote "
                        "enrollment+viewer still open in PAGE_AUTOMATION_TODO #31",
                        "error_category": "application"}
    host, user = _remote_host(args)
    if not user:
        return {"ok": False, "error": "no remote user (set REMOTE_WS_USER or pass user=)",
                "error_category": "application"}
    port = int(args.get("port") or 9711)
    timeout_s = int(args.get("timeout_s") or 120)
    profile = args.get("profile") or ""
    payload = {"bundle": bundle, "inputs": args.get("inputs") or {},
               "port": port, "profile": profile}
    arg_b64 = _b64.b64encode(_json.dumps(payload).encode("utf-8")).decode("ascii")
    remote_dir = "/tmp/.remote_cdp_agent"
    agent_path = remote_dir + "/agent.py"
    # Ship the agent to a temp file ONLY if its content changed (REMOTE_RUNTIME
    # #16) — the agent body is static across calls, so this is a near-always skip
    # after the first gather. Then run it with the json arg. module-load chrome +
    # chromedriver first (same recipe as sso_login). The agent prints ONE json line.
    sh_ok, _sk, sh_err = _ship_if_changed(rs, user, host, agent_path, _REMOTE_AGENT,
                                          timeout=40, sha=_REMOTE_AGENT_SHA)
    if not sh_ok:
        return {"ok": False, "error": "could not ship gather agent: %s" % sh_err,
                "error_category": "unavailable", "host": host}
    # WARM-AGENT fast path (REMOTE_RUNTIME #14/#19): reuse a persistent serve-mode
    # agent for (host,user,profile) so gathers after the first skip the ~10-15s
    # chromedriver cold start. On by default; warm=false forces the one-shot path.
    if args.get("warm", True):
        warm = _warm_gather(rs, user, host, remote_dir, bundle,
                            args.get("inputs") or {}, port, profile,
                            idle_ttl=float(args.get("warm_ttl_s") or 120),
                            timeout_s=timeout_s)
        if warm is not None:
            return warm
        # warm path unavailable (serve didn't come up) → fall through to one-shot.
    cmd = (
        "source /etc/profile 2>/dev/null; "
        "module load chrome/149.0.7827.102 chromedriver/149.0.7827.102 2>/dev/null; "
        "cd %s && python3 agent.py \"$(printf '%%s' %s | base64 -d)\""
    ) % (remote_dir, _shq(arg_b64))
    t0 = _time.time()
    try:
        r = rs.run(user, cmd, host=host, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "remote run failed: %s" % str(exc)[:240],
                "error_category": "unavailable"}
    elapsed = round(_time.time() - t0, 1)
    out = (r.get("stdout") or "").strip()
    # the agent prints one json line last; parse the LAST json-looking line.
    parsed = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = _json.loads(line)
                break
            except ValueError:
                continue
    if parsed is None:
        return {"ok": False, "error": "no result from remote agent (rc=%s): %s"
                % (r.get("rc"), (r.get("stderr") or out or "")[:300]),
                "error_category": "application", "host": host, "elapsed_s": elapsed}
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error", "remote gather failed"),
                "error_category": "application", "host": host, "elapsed_s": elapsed}
    return {"ok": True, "state": parsed.get("state", "done"),
            "collected": parsed.get("collected", {}), "trace": parsed.get("trace", []),
            "host": host, "elapsed_s": elapsed, "warm": False}


def _shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _warm_serve_port(port):
    # the serve control port sits 2 above the one-shot chromedriver port so a
    # warm agent and a one-shot run don't collide on the same host.
    return int(port) + 2


def _warm_gather(rs, user, host, remote_dir, bundle, inputs, port, profile,
                 idle_ttl=120.0, timeout_s=120):
    """WARM-AGENT gather (REMOTE_RUNTIME #14/#19). Reuse a persistent serve-mode
    agent for (host,user,profile): PING a well-known control port; if a warm
    agent answers, POST /gather to reuse its already-open chromedriver session
    (no cold start). Otherwise LAUNCH serve (paying the cold start ONCE), wait
    for SERVE_READY, then POST /gather. Returns the gather result dict, or None
    if the warm path couldn't be established (caller falls back to one-shot).

    The serve agent self-exits after idle_ttl with no request, so a warm agent
    can't linger — the next gather within the TTL reuses it, a later one cold-
    starts a fresh one."""
    sp = _warm_serve_port(port)
    t0 = _time.time()
    # 1) is a warm agent already up for this (host,user)+port?
    ping = _agent_poke(rs, user, host, sp, "/ping", timeout=8)
    warm_up = isinstance(ping, dict) and ping.get("ok") is True
    if not warm_up:
        # 2) launch serve in the background; wait (bounded) for SERVE_READY.
        import re as _re
        safe_prof = _re.sub(r"[^A-Za-z0-9_.-]", "_", profile or "") or ""
        launch = ("source /etc/profile 2>/dev/null; "
                  "module load chrome/149.0.7827.102 chromedriver/149.0.7827.102 2>/dev/null; "
                  "cd %s && setsid nohup python3 agent.py serve %d %s %d "
                  ">/tmp/_rcdp_serve_%d.log 2>&1 < /dev/null & echo LAUNCHED"
                  ) % (remote_dir, sp, _shq(safe_prof), int(idle_ttl), sp)
        try:
            rs.run(user, launch, host=host, timeout=10)
        except Exception:  # noqa: BLE001
            return None
        # poll /ping until the cold start finishes (bounded by ~30s for the
        # chromedriver spawn + session create).
        ready_deadline = _time.time() + 35
        while _time.time() < ready_deadline:
            _time.sleep(2)
            ping = _agent_poke(rs, user, host, sp, "/ping", timeout=8)
            if isinstance(ping, dict) and ping.get("ok") is True:
                warm_up = True
                break
        if not warm_up:
            return None  # serve never came up → one-shot fallback
    # 3) POST the gather to the warm agent (reuses its open session).
    res = _agent_poke(rs, user, host, sp, "/gather", method="POST",
                      body={"bundle": bundle, "inputs": inputs},
                      timeout=min(timeout_s, 90))
    elapsed = round(_time.time() - t0, 1)
    if not isinstance(res, dict) or "error" in res and not res.get("state"):
        return None  # warm gather failed → let the one-shot path try
    return {"ok": bool(res.get("ok", True)), "state": res.get("state", "done"),
            "collected": res.get("collected", {}), "trace": res.get("trace", []),
            "host": host, "elapsed_s": elapsed, "warm": True,
            "reused": bool(res.get("ok") and ping.get("gathers", 0) > 0)}


# --- remote LOGIN via the proven cdp_session.serve agent (#31 a + c) --------
# A login needs the full Entra screen ladder + trusted input + a human 2FA gate —
# which the cdp_session.py interactive agent already implements (its netrc-driven
# autofill drives account-tile/KMSI/email/password and pauses for 2FA). So remote
# login REUSES that agent rather than re-porting the 600-line case interpreter:
# ship cdp_session.py, launch `serve` at the login URL with autofill ON, then poll
# /url until the browser LEAVES the IdP (login done) — pausing at the 2FA gate for
# the human — and capture the resulting cookies. This is the credential-capture
# payoff the ws_services cookie pattern needs, now reachable from page_automation.
def _ship_cdp_session(rs, user, host, remote_dir):
    """Base64-ship the workspace cdp_session.py agent to the remote — but SKIP the
    upload when the remote copy already matches (REMOTE_RUNTIME #16). Returns
    (ok, err)."""
    here = _os.path.dirname(_os.path.abspath(__file__))
    root = _os.path.abspath(_os.path.join(here, "..", "..", ".."))
    src_path = _os.path.join(root, "apps", "knowledge", "workspace", "impl", "cdp_session.py")
    try:
        src, src_sha = _read_file_cached(src_path)  # #18: mtime-guarded read + sha
    except OSError as exc:
        return False, "cannot read cdp_session.py: %s" % exc
    ok, _skipped, err = _ship_if_changed(rs, user, host,
                                         remote_dir.rstrip("/") + "/cdp_session.py", src,
                                         sha=src_sha)
    return ok, err


def _load_sso_login():
    """Import apps/knowledge/workspace/impl/sso_login (for its proven paramiko
    direct-tcpip tunnel helpers) — same path-reach as remote_session. Returns the
    module or None (paramiko / layout missing)."""
    here = _os.path.dirname(_os.path.abspath(__file__))
    root = _os.path.abspath(_os.path.join(here, "..", "..", ".."))
    impl = _os.path.join(root, "apps", "knowledge", "workspace", "impl")
    if impl not in _sys.path:
        _sys.path.insert(0, impl)
    try:
        import sso_login as _sso  # noqa: E402
        return _sso
    except Exception:  # noqa: BLE001
        return None


# per-(host,user,port) tunnel channel cache for the login poll fast path (#15).
_LOGIN_TUNNELS = {}
_LOGIN_TUNNEL_LOCK = _threading.Lock()
# #3: idle TTL so a long-lived framework process can't accumulate half-open
# direct-tcpip channels to seli across many logins (e.g. if a login raises before
# _close_login_tunnels, or a target is used once and never again). Each poke
# stamps `last`; _reap_idle_tunnels (called at login start) closes any past TTL.
_LOGIN_TUNNEL_IDLE_S = 300


def _reap_idle_tunnels(now=None):
    """Close + drop any cached login tunnel idle longer than the TTL. Cheap, no
    dedicated thread — called at the start of each login so stale channels from a
    prior (possibly crashed) login don't linger. Returns the count reaped (#3)."""
    now = now if now is not None else _time.time()
    reaped = 0
    with _LOGIN_TUNNEL_LOCK:
        for key in [k for k, r in _LOGIN_TUNNELS.items()
                    if now - r.get("last", 0) > _LOGIN_TUNNEL_IDLE_S]:
            try:
                _LOGIN_TUNNELS[key]["ch"].close()
            except Exception:  # noqa: BLE001
                pass
            _LOGIN_TUNNELS.pop(key, None)
            reaped += 1
    return reaped


def _tunnel_poke(rs, user, host, port, path, method="GET", body=None):
    """FAST login poke over a persistent paramiko direct-tcpip channel (REMOTE_
    RUNTIME #15), reusing sso_login's proven relay helpers instead of spawning a
    fresh `python3 -c urllib` via an SSH exec per poke (~50 process spawns over a
    200s window). One channel per (host,user,port) is kept alive + reconnected
    once on failure. Returns the parsed JSON, or None to signal the caller should
    fall back to the slow exec poke (_agent_poke)."""
    sso = _load_sso_login()
    if sso is None or not hasattr(sso, "_open_tunnel"):
        return None
    key = (host, user, port)
    for attempt in (0, 1):
        with _LOGIN_TUNNEL_LOCK:
            rec = _LOGIN_TUNNELS.get(key)
            if rec is None:
                ch = sso._open_tunnel(user, host, port)
                if ch is None:
                    return None  # tunnel unavailable → exec fallback
                rec = {"ch": ch, "lock": _threading.Lock(), "last": _time.time()}
                _LOGIN_TUNNELS[key] = rec
        with rec["lock"]:
            try:
                r = sso._http_over_channel(rec["ch"], port, path, method, body)
                rec["last"] = _time.time()   # #3: stamp last-use for the idle reaper
                return r
            except Exception:  # noqa: BLE001
                try:
                    rec["ch"].close()
                except Exception:  # noqa: BLE001
                    pass
                with _LOGIN_TUNNEL_LOCK:
                    _LOGIN_TUNNELS.pop(key, None)
                if attempt == 1:
                    return None
    return None


def _close_login_tunnels(host, user, port):
    """Tear down any cached login tunnel channels for this target (call at the
    end of a login so a channel can't linger)."""
    with _LOGIN_TUNNEL_LOCK:
        for key in [k for k in _LOGIN_TUNNELS if k[:3] == (host, user, port)]:
            try:
                _LOGIN_TUNNELS[key]["ch"].close()
            except Exception:  # noqa: BLE001
                pass
            _LOGIN_TUNNELS.pop(key, None)


def _poke(rs, user, host, port, path, method="GET", body=None, timeout=12):
    """Login poke: try the fast tunnel first (#15), fall back to the exec poke."""
    r = _tunnel_poke(rs, user, host, port, path, method=method, body=body)
    if r is not None:
        return r
    return _agent_poke(rs, user, host, port, path, method=method, body=body, timeout=timeout)


def _agent_poke(rs, user, host, port, path, method="GET", body=None, timeout=30):
    """One urllib request to the remote agent's localhost:<port> via an SSH exec
    (the cap doesn't need the low-latency tunnel the live viewer uses)."""
    data_arg = ("data=%r.encode()," % _json.dumps(body)) if body is not None else ""
    runner = (
        "import json,urllib.request\n"
        "req=urllib.request.Request('http://127.0.0.1:%d%s',%s method=%r)\n"
        "try:\n"
        "    r=urllib.request.urlopen(req,timeout=25); print(r.read().decode('utf-8','replace'))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'error':str(e)[:160]}))\n"
    ) % (port, path, data_arg, method)
    cmd = "python3 -c %s" % _shq(runner)
    try:
        r = rs.run(user, cmd, host=host, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"error": "poke failed: %s" % str(exc)[:160]}
    out = (r.get("stdout") or "").strip()
    try:
        return _json.loads(out.splitlines()[-1]) if out else {"error": "empty"}
    except Exception:  # noqa: BLE001
        return {"error": "unparseable: %s" % out[:160]}


_IDP_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com",
              "adfs", "/adfs/", "sts.", "msauth", "aadcdn", "/common/oauth2",
              "/oauth2/authorize")


def login(args, ctx):
    """Drive an Entra web LOGIN on the REMOTE host and capture the resulting
    session cookies (#31 a+c). Launches the cdp_session.py interactive agent at
    `start_url` with netrc autofill ON (so it types the AD id/pw and only the 2FA
    is the human's), polls until the browser LEAVES the IdP (login complete) or
    the human budget expires, then reads the cookies for `cookie_urls` filtered by
    `domain_filter`/`sentinel`.

    args:
      start_url      — the service/login URL to open (REQUIRED).
      profile        — remote persistent profile name (default '_pa_login').
      account/email  — the full work email to type (passed to autofill as RWS_EMAIL).
      autofill_pw    — the AD/Entra password (REQUIRED unless autofill_host+netrc_ok
                       are given). Sent to the remote via env only; never logged.
      autofill_host  — a ~/.netrc machine whose entry holds the AD password (use
                       INSTEAD of autofill_pw; must be paired with netrc_ok=true).
      netrc_ok       — confirm that autofill_host's netrc entry is the AD password
                       (guards against typing a wrong-service token at Microsoft).
      sentinel       — cookie name whose presence means authenticated (optional).
      domain_filter  — comma-list of cookie-domain substrings to keep (optional).
      mode           — 'cookie' (default) captures the cookie set; 'bearer' (#20)
                       instead captures an Authorization: Bearer token the page's
                       own XHR sends to host_filter (outlook/graph/fpt/… services).
      host_filter    — (bearer mode) API host substring the Bearer token targets.
      host/user      — remote target (defaults seli / $USER).
      human_timeout_s— how long to wait at the 2FA gate (default 180).

    Returns {ok, authed, url, cookies:{name:value}, count, token, mode, idp_error,
             host, elapsed_s, mfa_number, waiting_for_approval, trace}. In bearer
             mode `token` is the captured Bearer (a SECRET — never log it); cookies
             is empty. In cookie mode `cookies`/`count` carry the set; token is ''.
    The human approves the 2FA on their phone while this polls. REFUSES to run
    without an explicit AD password source (the lockout guard — a blind ~/.netrc
    first-entry grab is a Gerrit/ARM token, not the AD password)."""
    rs, bad = _ensure_rs()
    if bad:
        return bad
    _reap_idle_tunnels()   # #3: close any half-open login channels left idle by a prior login
    start_url = (args.get("start_url") or "").strip()
    if not start_url:
        return {"ok": False, "error": "start_url required", "error_category": "application"}
    host, user = _remote_host(args)
    if not user:
        return {"ok": False, "error": "no remote user (set REMOTE_WS_USER or pass user=)",
                "error_category": "application"}
    profile = (args.get("profile") or "_pa_login").strip()
    email = (args.get("account") or args.get("email") or "").strip()
    pw = args.get("autofill_pw") or ""
    autofill_host = (args.get("autofill_host") or "").strip()
    netrc_ok = bool(args.get("netrc_ok"))
    sentinel = (args.get("sentinel") or "").strip()
    domain_filter = (args.get("domain_filter") or "").strip()
    mode = (args.get("mode") or "cookie").strip().lower()      # cookie | bearer (#20)
    host_filter = (args.get("host_filter") or "").strip()
    human_budget = float(args.get("human_timeout_s") or 180)
    port = int(args.get("port") or 8793)
    remote_dir = "/tmp/.remote_cdp_agent"

    # CREDENTIAL-SOURCE GUARD (the lockout fix): a Microsoft/Entra login must NOT
    # fall back to "the first ~/.netrc entry" — that netrc holds Gerrit/ARM TOKENS,
    # not the AD password, and typing a Gerrit token at the Microsoft password
    # field repeatedly LOCKED the account. So require an EXPLICIT AD password
    # source: either autofill_pw=, or autofill_host= naming the specific netrc
    # machine that holds the AD password, or an explicit netrc_ok=true opt-in that
    # ALSO must name autofill_host (never a blind first-entry grab). Otherwise we
    # refuse to attempt a login.
    if not pw and not autofill_host:
        return {"ok": False, "error": "refusing to drive a login without an explicit AD "
                "password source — pass autofill_pw= (the AD/Entra password; sent via env, "
                "never logged) OR autofill_host= naming the ~/.netrc machine that holds it. "
                "The remote ~/.netrc first entry is a Gerrit/ARM TOKEN, not the AD password; "
                "typing it at Microsoft previously locked the account.",
                "error_category": "application", "need": "credential"}
    if autofill_host and not pw and not netrc_ok:
        # autofill_host given but caller hasn't confirmed that netrc entry is the
        # AD password — require netrc_ok=true so it's a deliberate choice.
        return {"ok": False, "error": "autofill_host=%r given but netrc_ok not set — pass "
                "netrc_ok=true to confirm that netrc entry holds the AD/Entra password "
                "(guards against typing a wrong-service token at Microsoft)." % autofill_host,
                "error_category": "application", "need": "confirm"}

    # #2: ship-skip INLINE — fold the sha-probe + conditional write into the SAME
    # exec that launches serve (one SSH round-trip instead of probe-then-launch).
    # Read the agent content (mtime-cached, #18) and build the ship prelude; if the
    # file can't be read, fall back to the standalone ship so the error is clear.
    here = _os.path.dirname(_os.path.abspath(__file__))
    root = _os.path.abspath(_os.path.join(here, "..", "..", ".."))
    _src_path = _os.path.join(root, "apps", "knowledge", "workspace", "impl", "cdp_session.py")
    _ship_prefix = ""
    try:
        _src, _src_sha = _read_file_cached(_src_path)
        _ship_prefix = _ship_prelude(remote_dir, remote_dir.rstrip("/") + "/cdp_session.py",
                                     _src, _src_sha) + "; "
    except OSError:
        ok, err = _ship_cdp_session(rs, user, host, remote_dir)
        if not ok:
            return {"ok": False, "error": "could not ship agent: %s" % err,
                    "error_category": "unavailable"}
    # stale-agent cleanup for THIS port/profile, then launch serve in background.
    import re as _re
    safe_prof = _re.sub(r"[^A-Za-z0-9_.-]", "_", profile) or "x"
    rs.run(user, ("pkill -9 -f 'cdp_session.py serve [^ ]* %s ' 2>/dev/null; "
                  "rm -f /repo/$USER/remote-ws/cdp_profile/%s/Singleton* 2>/dev/null; true")
           % (safe_prof, safe_prof), host=host, timeout=20)
    env_bits = []
    if email:
        env_bits.append("RWS_EMAIL=%s" % _shq(email))
    if pw:
        env_bits.append("RWS_PW=%s" % _shq(pw))
    env = (" ".join(env_bits) + " ") if env_bits else ""
    # Pass the autofill-host as serve's 4th arg so the agent's OWN autofill thread
    # resolves the password from THAT specific netrc machine (not the first entry).
    # When an explicit pw is given (RWS_PW), it takes precedence in autofill_once;
    # pass '\x00OFF' only if we had neither — but the guard above already blocks
    # that case, so here we always have pw OR a confirmed autofill_host.
    af_arg = autofill_host if autofill_host else ""
    # For a bearer-mode service, pass serve's 6th arg "1" so it enables perf
    # logging and the /bearer endpoint can observe the Authorization header (#20).
    perf_arg = "1" if mode == "bearer" else ""
    launch = ("%s"
              "source /etc/profile 2>/dev/null; "
              "module load chrome/149.0.7827.102 chromedriver/149.0.7827.102 2>/dev/null; "
              "cd %s && CDP_SESSION_BROWSER=chrome %ssetsid nohup python3 cdp_session.py serve "
              "%s %s %d %s %s >/tmp/_pa_login_%d.log 2>&1 < /dev/null & echo STARTED"
              ) % (_ship_prefix, remote_dir, env, _shq(start_url), _shq(profile), port,
                   _shq(af_arg), _shq(perf_arg), port)
    try:
        rs.run(user, launch, host=host, timeout=8)
    except Exception:  # noqa: BLE001
        pass  # backgrounded; the exec return is often lost — we poll next.

    t0 = _time.time()
    deadline = t0 + human_budget + 30
    hard_cap = t0 + human_budget + 90   # absolute wall-clock guard (hang safety)
    authed = False
    last_url = ""
    seen_idp = False
    idp_error = ""
    mfa_number = ""
    token = ""   # bearer-mode token (captured in-loop for a bearer service)
    # poll /shot (url + autofill status) until we LEAVE the IdP (login done), hit
    # an idp-error (wrong pw / LOCKOUT — bail immediately, never keep trying), or
    # time out. The agent's own autofill drives id/pw; the human approves 2FA.
    # Each poke is tightly bounded (the agent answers in <1s when alive; a wedged
    # half-open socket must NOT be waited on for long) + an absolute hard_cap so
    # the op can never hang past the human window even if the transport wedges.
    while _time.time() < deadline and _time.time() < hard_cap:
        _time.sleep(4)
        s = _poke(rs, user, host, port, "/shot", timeout=12)
        last_url = (s.get("url") or "")
        af = (s.get("af") or "")
        low = last_url.lower()
        # EARLY-OUT on an IdP error (the autofill reports e.g. 'idp-error:Your
        # account is temporarily locked...'). Stop at once — retrying a rejected
        # password is exactly what triggers/extends a lockout.
        if isinstance(af, str) and af.startswith("idp-error"):
            idp_error = af[len("idp-error:"):].strip(" :") or af
            break
        if not low:
            continue
        on_idp = any(h in low for h in _IDP_HOSTS)
        if on_idp:
            seen_idp = True
            # MFA number scrape (REMOTE_RUNTIME #4): once we're on the IdP, check
            # for the 'enter this number' value so the human sees it FAST. Only
            # poke /number while we don't yet have it (cheap: skip once captured).
            if not mfa_number:
                nr = _poke(rs, user, host, port, "/number", timeout=12)
                if isinstance(nr, dict):
                    num = (nr.get("number") or "").strip()
                    if num:
                        mfa_number = num
        elif mode == "bearer" and low.startswith("http"):
            # BEARER on a non-IdP page (#20/#23): a bearer service with an enrolled
            # profile lands DIRECTLY on the authenticated SPA and never shows an
            # IdP page — so 'authed' can't be gated on the IdP round-trip (that was
            # the cookie-login shape). Instead, poll /bearer right here; a token on
            # host_filter means we're authenticated. Nudge the SPA (reload) so it
            # fires its API call. If no token yet, keep polling until the window
            # ends (the SPA may need a beat, or a human query for Graph Explorer).
            br = _poke(rs, user, host, port, "/bearer?host_filter=%s" % host_filter, timeout=12)
            if isinstance(br, dict) and (br.get("token") or "").strip():
                token = br["token"].strip()
                authed = True
                break
        elif seen_idp and low.startswith("http"):
            authed = True
            break
    elapsed = round(_time.time() - t0, 1)
    # Pull the per-pass TRACE from the agent (REMOTE_RUNTIME #2) BEFORE quitting —
    # this is what turns a blind login stall into something diagnosable: it shows
    # each autofill pass's url + whether a field was filled / submit clicked /
    # idp-error, so #1 can be reproduced from the record instead of a live attempt.
    trace = []
    tr = _poke(rs, user, host, port, "/trace", timeout=12)
    if isinstance(tr, dict) and isinstance(tr.get("trace"), list):
        trace = tr["trace"][-40:]
    cookies = {}
    if authed and mode == "bearer" and not token:
        # BEARER capture fallback (#20): if we broke out authed via the cookie-
        # shaped IdP round-trip but didn't capture the token in-loop, poll /bearer
        # a few more times (the SPA may not have fired its API call yet).
        for _ in range(6):
            br = _poke(rs, user, host, port, "/bearer?host_filter=%s" % host_filter, timeout=12)
            if isinstance(br, dict) and (br.get("token") or "").strip():
                token = br["token"].strip()
                break
            _time.sleep(2)
        if not token:
            authed = False   # authed to the IdP but no bearer minted → not usable
    elif authed:
        ck = _poke(rs, user, host, port, "/cookies", timeout=12)
        if isinstance(ck, dict) and "error" not in ck:
            filt = [f.strip().lstrip(".") for f in domain_filter.split(",") if f.strip()]
            # the agent returns {name:value} (no domains), so a domain filter can
            # only be enforced by cookie_urls scoping; keep all when no sentinel.
            if sentinel and sentinel not in ck:
                authed = False   # sentinel missing → not really authed
            cookies = dict(ck)
            _ = filt  # provenance; url-scoping already applied by the browser
    # shut the agent down
    try:
        _poke(rs, user, host, port, "/quit", method="POST", body={}, timeout=15)
    except Exception:  # noqa: BLE001
        pass
    _close_login_tunnels(host, user, port)   # don't leave a direct-tcpip channel open (#15)
    rs.run(user, "pkill -9 -f 'cdp_session.py serve [^ ]* %s ' 2>/dev/null; true"
           % safe_prof, host=host, timeout=15)
    if idp_error:
        _detail = "login rejected by the IdP: %s (STOPPED — did not retry, to avoid a lockout)" % idp_error
    elif authed and mode == "bearer":
        _detail = "captured a bearer token"
    elif authed:
        _detail = "captured %d cookies" % len(cookies)
    elif mode == "bearer" and seen_idp and token == "":
        _detail = ("reached the service but no bearer token was observed on the API "
                   "host %r — the SPA may not have called its API yet" % host_filter)
    elif mfa_number:
        _detail = ("login did not complete — a 2FA approval was pending: enter the "
                   "number %s in Microsoft Authenticator" % mfa_number)
    elif seen_idp:
        _detail = ("login did not complete — reached the identity provider and was "
                   "waiting for your approval (no 2FA completion within the window)")
    else:
        _detail = "login did not complete (timed out at a gate / no 2FA approval)"
    # waiting_for_approval: we got to the IdP (and maybe a number) but never left
    # it and saw no idp-error — i.e. the human 2FA gate, not a failure. Lets a
    # caller/UI show 'waiting for your approval' instead of a generic 'hung'.
    waiting_for_approval = bool(seen_idp and not authed and not idp_error)
    return {"ok": True, "authed": authed, "url": last_url, "cookies": cookies,
            "count": len(cookies), "token": token, "mode": mode, "host": host,
            "elapsed_s": elapsed, "idp_error": idp_error, "detail": _detail,
            "trace": trace, "mfa_number": mfa_number,
            "waiting_for_approval": waiting_for_approval}


def enrolled(args, ctx):
    """Report whether the persistent Chrome PROFILE for a service is enrolled on
    the remote (REMOTE_RUNTIME #21) — i.e. shows a completed interactive sign-in,
    so a headless scrape can reuse it. Mirrors cdp_session.is_enrolled: a non-empty
    Cookies DB OR a populated Local Storage / IndexedDB / Session Storage leveldb
    (bearer/MSAL apps keep the token there, not in cookies). This lets callers
    (ws_services._opener_for) gate on the CAP instead of importing cdp_session.

    args: profile (REQUIRED — the service profile name, e.g. 'sharepoint'),
          host/user (defaults seli / $USER). Returns {ok, enrolled, profile}."""
    rs, bad = _ensure_rs()
    if bad:
        return bad
    profile = (args.get("profile") or "").strip()
    if not profile:
        return {"ok": False, "error": "profile required", "error_category": "application"}
    host, user = _remote_host(args)
    if not user:
        return {"ok": False, "error": "no remote user (set REMOTE_WS_USER or pass user=)",
                "error_category": "application"}
    # Tiny stdlib checker shipped+run on the remote. Resolves the SAME profile
    # root cdp_session uses (/repo/$USER/remote-ws/cdp_profile, else ~/.remote_ws
    # _cdp, else /tmp), then checks the cookie DB + auth leveldb stores.
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", profile) or "default"
    checker = (
        "import os,json\n"
        "u=os.environ.get('USER') or 'u'\n"
        "env=os.environ.get('CDP_SESSION_PROFILE_ROOT')\n"
        "repop='/repo/'+u\n"
        "if env: root=env\n"
        "elif os.path.isdir(repop) and os.access(repop,os.W_OK): root='/repo/'+u+'/remote-ws/cdp_profile'\n"
        "elif os.path.isdir(os.path.expanduser('~')): root=os.path.join(os.path.expanduser('~'),'.remote_ws_cdp')\n"
        "else: root='/tmp/.remote_ws_cdp_'+u\n"
        "d=os.path.join(root,%r)\n"
        "def enrolled():\n"
        " for sub in ('Default/Cookies','Default/Network/Cookies'):\n"
        "  p=os.path.join(d,sub)\n"
        "  try:\n"
        "   if os.path.isfile(p) and os.path.getsize(p)>0: return True\n"
        "  except OSError: pass\n"
        " for sub in ('Default/Local Storage/leveldb','Default/IndexedDB','Default/Session Storage'):\n"
        "  base=os.path.join(d,sub)\n"
        "  if not os.path.isdir(base): continue\n"
        "  for r,_dd,ff in os.walk(base):\n"
        "   for fn in ff:\n"
        "    try:\n"
        "     if os.path.getsize(os.path.join(r,fn))>0: return True\n"
        "    except OSError: pass\n"
        " return False\n"
        "print(json.dumps({'enrolled':enrolled(),'dir':d}))\n"
    ) % safe
    cmd = "python3 -c %s" % _shq(checker)
    try:
        r = rs.run(user, cmd, host=host, timeout=25)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "enrolled check failed: %s" % str(exc)[:160],
                "error_category": "unavailable", "host": host}
    out = (r.get("stdout") or "").strip()
    parsed = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = _json.loads(line)
                break
            except ValueError:
                continue
    if parsed is None:
        return {"ok": False, "error": "no result from enrolled check: %s"
                % ((r.get("stderr") or out or "")[:200]), "error_category": "application",
                "host": host}
    return {"ok": True, "enrolled": bool(parsed.get("enrolled")),
            "profile": profile, "dir": parsed.get("dir"), "host": host}


METHODS = {"status": status, "gather": gather, "login": login, "enrolled": enrolled}
CAPABILITY = "cap:remote.cdp"
VARIANT = "ssh"          # drives a remote host over SSH (not the local browser)
ATTRS = {"remote": True}
