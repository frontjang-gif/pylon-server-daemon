"""capability: cap:system.browser.cookie  (Windows, via the signed-in Edge + CDP).

Endpoint-bound: extracts SESSION COOKIES for a domain from the user's signed-in
Edge via CDP Network.getCookies — the browser already holds the Cloudflare/SSO
session, so a credential-injected HTTP fetch can reuse those cookies for sites
that gate on them (confluence / etsi / cloudflare-fronted apps / google_keep).
The credential app calls this for a cookie-type credential; it never drives CDP
itself.

How: open the site URL in a tab (so the SSO handshake completes in the real
browser session), then Network.getCookies for that origin, join into a Cookie
header string, and close the tab. Optionally reuse an already-open tab.

Methods:
  get(url, stage=True, settle_s=5, tab_id?) -> {ok, domain, cookie, count}
      `cookie` is a ready-to-send 'name=value; name=value' header string.

Reuses _cdp_common. ctx.user maps to the one local browser (single-user today).
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from . import _cdp_common as cdp


def _origin(url):
    p = urlparse(url)
    return p.hostname or "", f"{p.scheme}://{p.hostname}" if p.hostname else ""


def get(args, ctx):
    cdp = globals()['cdp']
    if args.get('profile'):
        from ._browser_profiles import Client
        cdp = Client(str(args['profile']))
    bad = cdp.ensure_available()
    if bad:
        return bad
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url required", "error_category": "application"}
    domain, base = _origin(url)
    if not domain:
        return {"ok": False, "error": "could not parse domain from url",
                "error_category": "application"}
    stage = args.get("stage", True)
    settle = float(args.get("settle_s", 5))
    tab_id = args.get("tab_id")
    opened = None
    try:
        if tab_id:
            target = cdp.target_by_id(tab_id)
        elif stage:
            o = cdp.open_tab(url)
            opened = o.get("id")
            target = cdp.wait_for_ws(opened, timeout=10)
            if settle > 0:
                time.sleep(settle)   # let SSO / Cloudflare complete in the real session
            target = cdp.target_by_id(opened) or target
        else:
            # reuse any open tab already on that origin
            target = next((t for t in cdp.page_tabs() if domain in (t.get("url") or "")), None)
        if not target or "webSocketDebuggerUrl" not in target:
            return {"ok": False, "error": f"no tab available for {domain} "
                    "(pass stage=true or a tab_id)", "error_category": "unavailable"}
        ws = cdp.connect_ws(target["webSocketDebuggerUrl"])
        try:
            res = cdp.ws_send_recv(ws, "Network.getCookies", {"urls": [base]}, timeout=10)
        finally:
            ws.close()
        cookies = res.get("cookies", []) or []
        if not cookies:
            return {"ok": False,
                    "error": f"no cookies for {domain} (a manual SSO login may be needed)",
                    "error_category": "unavailable"}
        cookie_str = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies
                               if c.get("name") and c.get("value"))
        return {"ok": True, "domain": domain, "cookie": cookie_str, "count": len(cookies),
                "expires": _expiry_map(cookies)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    finally:
        if opened and not args.get("keep_tab"):
            cdp.close_tab(opened)


METHODS = {"get": get}   # + import_from_browser (registered below, after its definition)
CAPABILITY = "cap:system.browser.cookie"
VARIANT = "sso"
ATTRS = {"sso": True}


def _expiry_map(cookies):
    """{name: expires_epoch} for persistent cookies (session cookies = -1 are omitted).
    Not secret: lets callers report session age/expiry without the values."""
    out = {}
    for c in cookies:
        e = c.get("expires")
        if c.get("name") and isinstance(e, (int, float)) and e > 0:
            out[c["name"]] = min(out.get(c["name"], e), e)
    return out


def _domain_ok(host, domains):
    host = (host or "").lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _shared_copy(src, dst):
    """Copy a file that another process (the running browser) holds open. Chromium
    opens its cookie DB with a restrictive share mode, so shutil.copy2 gets
    PermissionError; CreateFile with FILE_SHARE_READ|WRITE|DELETE succeeds."""
    import os
    import shutil
    try:
        shutil.copy2(src, dst)
        return
    except PermissionError:
        if os.name != "nt":
            raise
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.CreateFileW(src, 0x80000000, 0x7, None, 3, 0x80, None)   # GENERIC_READ, share all, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError("CreateFile failed (%d)" % ctypes.get_last_error())
    try:
        buf = ctypes.create_string_buffer(1 << 20)
        n = wintypes.DWORD(0)
        with open(dst, "wb") as out:
            while k32.ReadFile(h, buf, len(buf), ctypes.byref(n), None) and n.value:
                out.write(buf.raw[:n.value])
    finally:
        k32.CloseHandle(h)


def import_from_browser(args, ctx):
    """Read session cookies for `domains` (default google.com) from the user's NORMAL,
    already-signed-in Edge profile — so NO login automation (and no rate-limit exposure).

    How: Chromium's cookie DB is encrypted (DPAPI / app-bound), but msedge.exe itself can
    decrypt it. So we COPY Local State + the profile's Cookies DB to a throw-away
    user-data-dir, launch a headless msedge on that copy, read the cookies over CDP
    (Storage.getCookies) and delete the copy. Read-only wrt the real profile.
    args: domains=[...], browser_profile="Default", settle_s=2.
    -> {ok, cookie (header string), count, names, domains, profile}"""
    import json
    import os
    import shutil
    import subprocess
    import tempfile
    import urllib.request
    from . import _edge_profile as edge

    domains = [str(d).lstrip(".").lower() for d in (args.get("domains") or ["google.com"]) if d]
    prof = (args.get("browser_profile") or "Default").strip()
    if not prof or any(c in prof for c in "\\/:") or prof.startswith("."):
        return {"ok": False, "error": "invalid browser_profile", "error_category": "application"}
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")
    if args.get("user_data_dir"):          # another Chromium user-data dir (tests / other installs)
        root = str(args["user_data_dir"])
    src_cookies = None
    for rel in (("Network", "Cookies"), ("Cookies",)):
        cand = os.path.join(root, prof, *rel)
        if os.path.isfile(cand):
            src_cookies = cand
            break
    if not src_cookies or not os.path.isfile(os.path.join(root, "Local State")):
        return {"ok": True, "imported": False, "locked": False,
                "error": "normal Edge profile %r not found" % prof}
    binary = edge._edge_binary()
    if not binary:
        return {"ok": False, "error": "Microsoft Edge was not found", "error_category": "unavailable"}
    tmp = tempfile.mkdtemp(prefix="pylon-cookie-import-")
    proc = None
    try:
        try:
            _shared_copy(os.path.join(root, "Local State"), os.path.join(tmp, "Local State"))
            dst_dir = os.path.join(tmp, "Default", "Network")
            os.makedirs(dst_dir, exist_ok=True)
            _shared_copy(src_cookies, os.path.join(dst_dir, "Cookies"))
            jr = src_cookies + "-journal"
            if os.path.isfile(jr):
                _shared_copy(jr, os.path.join(dst_dir, "Cookies-journal"))
        except OSError as exc:
            locked = "(32)" in str(exc) or getattr(exc, "winerror", None) == 32
            # an EXPECTED outcome (not a cap failure): ok:true/imported:false, so the
            # framework circuit breaker does not open on the cookie cap (that would
            # also break the regular `get` used by every Keep call).
            return {"ok": True, "imported": False, "locked": locked,
                    "error": ("the normal Edge is running and holds its cookie DB exclusively - close ALL "
                              "Edge windows (and disable Startup boost) once, then retry"
                              if locked else "could not copy the browser cookie DB: %s" % exc),
                    "error_category": "unavailable"}
        port = edge._free_port()
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.Popen(
            [binary, "--headless=new", "--user-data-dir=" + tmp, "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--disable-sync",
             "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=%d" % port,
             "--remote-allow-origins=*", "about:blank"],
            startupinfo=startup, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if edge._edge_alive_on(port):
                break
            if proc.poll() is not None:
                return {"ok": False, "error": "headless Edge exited early", "error_category": "unavailable"}
            time.sleep(.25)
        else:
            return {"ok": False, "error": "headless Edge did not start", "error_category": "unavailable"}
        time.sleep(float(args.get("settle_s", 2)))
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=5) as r:
            wsu = json.load(r)["webSocketDebuggerUrl"]
        ws = cdp.connect_ws(wsu, timeout=10)
        try:
            res = cdp.ws_send_recv(ws, "Storage.getCookies", {}, timeout=15)
        finally:
            ws.close()
        cookies = [c for c in (res.get("cookies") or []) if _domain_ok(c.get("domain"), domains)
                   and c.get("name") and c.get("value")]
        if not cookies:
            return {"ok": True, "imported": False, "locked": False,
                    "error": "no %s cookies in the normal Edge profile %r (not signed in, or "
                    "app-bound encryption blocked decryption)" % ("/".join(domains), prof)}
        # one value per name (prefer the most specific/longest-domain-match)
        by = {}
        for c in cookies:
            by.setdefault(c["name"], c["value"])
        return {"ok": True, "imported": True, "cookie": "; ".join("%s=%s" % kv for kv in by.items()),
                "count": len(by), "names": sorted(by), "domains": domains, "profile": prof,
                "expires": _expiry_map(cookies)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "cookie import failed: %s" % exc, "error_category": "unavailable"}
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


METHODS["import_from_browser"] = import_from_browser
