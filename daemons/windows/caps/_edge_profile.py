"""_edge_profile — a PERSISTENT, dedicated ai-pylon2 Edge profile for the browser
capabilities, SIGNED IN ONCE (interactively) and thereafter account-synced.

NOT a capability (the daemon skips `_`-prefixed modules). This owns a long-lived
Edge the browser caps (system.cdp / browser.token / browser.cookie /
browser.fetch) drive, so they don't depend on a pre-running debuggable Edge.

Design (corrected):
  * The profile lives under data/daemons/windows/edge-profile — a DEDICATED,
    PERSISTENT dir reused across daemon restarts.
  * It is a CLEAN profile Edge builds itself; it is NOT seeded by copying files
    from the real Edge profile. (Seeding produced a half-signed-in hybrid whose
    account_info stayed EMPTY and never synced — that was the MUM-SSO blocker.
    Legacy best-effort seeding is still available behind PYLON2_EDGE_SEED=1.)
  * On first non-headless launch Edge AUTO-ATTACHES the Windows work account via
    WAM SSO (account_info populated), and `cap:system.cdp.signin` lets the user
    confirm sign-in / enable Sync once. After that the live Entra session
    (incl. login.microsoftonline.com cookies) persists, so server-side OAuth
    redirects (e.g. the MUM authorize step) and token/cookie extraction complete
    SILENTLY — exactly like the old ai-pylon persistent profile.
  * SAME-MACHINE ONLY by nature: the WAM/TPM-held session is bound to THIS
    Windows user/host. That's exactly our case (the daemon runs on the user's box).

Launch: a persistent `msedge [--headless=new] --user-data-dir=<profile>
--remote-debugging-port=<port>`. Default headless for silent extraction;
ensure_mode(headless=False) flips it non-headless on demand for a gateway fetch
or the one-time sign-in. The chosen port is written to
data/daemons/windows/edge.port (mode to edge.mode) and returned so _cdp_common
points at it.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import socket
import subprocess
import time

# --- paths ------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))          # caps/
_DAEMON_DIR = os.path.dirname(_HERE)                        # daemons/windows
_REPO_ROOT = os.path.dirname(os.path.dirname(_DAEMON_DIR))  # C:\ai-pylon2
_DATA_ROOT = os.environ.get("PYLON2_DATA_DIR") or os.path.join(_REPO_ROOT, "data")
_OWNER_DATA = os.path.join(_DATA_ROOT, "daemons", "windows")

PROFILE_DIR = os.environ.get("PYLON2_EDGE_PROFILE") or os.path.join(_OWNER_DATA, "edge-profile")
PORT_FILE = os.path.join(_OWNER_DATA, "edge.port")
# Records whether the live managed Edge is running headless or not. A gateway
# host (e.g. the MUM portal) chrome-errors under headless and REQUIRES a
# non-headless Chromium — the browser.fetch cap uses ensure_mode() to relaunch
# the managed Edge non-headless on demand, and this sidecar lets a reused
# instance report its mode across daemon restarts.
MODE_FILE = os.path.join(_OWNER_DATA, "edge.mode")
# A persistent "prefer headed" latch. The Teams chat-sync path needs a HEADED
# Edge (headless Chromium throttles Teams' background sync, so its local chat
# list — our 1:1 source — goes stale). The token/cookie caps only READ
# localStorage, which works identically headed, so once anything latches headed
# we keep headed and never flip back to headless (which previously fought the
# Teams cap and starved its sync). Delete this file (or call set_prefer_headed
# (False)) to return to headless-by-default.
PREFER_HEADED_FILE = os.path.join(_OWNER_DATA, "edge.prefer_headed")


def prefer_headed():
    try:
        return os.path.isfile(PREFER_HEADED_FILE)
    except OSError:
        return False


def set_prefer_headed(on=True):
    try:
        os.makedirs(_OWNER_DATA, exist_ok=True)
        if on:
            with open(PREFER_HEADED_FILE, "w") as f:
                f.write("1")
        elif os.path.isfile(PREFER_HEADED_FILE):
            os.remove(PREFER_HEADED_FILE)
    except OSError:
        pass


def _edge_binary():
    for p in (os.environ.get("EDGE_PATH"),
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if p and os.path.isfile(p):
            return p
    return None


def _real_user_data():
    env = os.environ.get("EDGE_USER_DATA")
    if env:
        return env
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(local, "Microsoft", "Edge", "User Data")


# --- share-aware copy (Edge may be running and hold files open) -------------

def _copy_shared(src, dst):
    """Copy a file another process holds open (FILE_SHARE_READ|WRITE|DELETE)."""
    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    FILE_ATTR_NORMAL = 0x80
    INVALID = ctypes.c_void_p(-1).value
    CreateFileW = ctypes.windll.kernel32.CreateFileW
    CreateFileW.restype = ctypes.c_void_p
    h = CreateFileW(ctypes.c_wchar_p(src), GENERIC_READ, FILE_SHARE_ALL, None,
                    OPEN_EXISTING, FILE_ATTR_NORMAL, None)
    if not h or h == INVALID:
        return False
    try:
        ReadFile = ctypes.windll.kernel32.ReadFile
        buf = ctypes.create_string_buffer(1024 * 1024)
        nread = ctypes.wintypes.DWORD(0)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as out:
            while True:
                ok = ReadFile(ctypes.c_void_p(h), buf, len(buf), ctypes.byref(nread), None)
                if not ok or nread.value == 0:
                    break
                out.write(buf.raw[:nread.value])
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


def _copy_one(src, dst):
    if not os.path.isfile(src):
        return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fo.write(fi.read())
        return True
    except (OSError, PermissionError):
        return _copy_shared(src, dst)


def _copytree_live(src, dst):
    copied = 0
    skip = {"lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"}
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out_root = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(out_root, exist_ok=True)
        for fn in files:
            if fn in skip:
                continue
            if _copy_one(os.path.join(root, fn), os.path.join(out_root, fn)):
                copied += 1
    return copied


# The session FLOOR that makes a cloned profile AUTO-LOGIN (the remote_ws
# edge_capture clone technique): the account pointer + tamper MAC + the DPAPI
# os_crypt key. The clone does NOT carry Network/Cookies (Chromium locks it while
# running and it CANNOT be copied live) — instead the carried primary-refresh-
# token silently RE-MINTS the service cookies on the clone's first navigation.
# Plus Local/Session Storage + identity IndexedDB for app-layer sessions.
_SEED_PROFILE_FILES = ("Secure Preferences", "Preferences",
                       "Web Data", "Login Data")
_SEED_STORAGE_DIRS = ("Local Storage", "Session Storage")
_AUTH_ORIGINS = ("login.microsoftonline", "login.live", "login.microsoft", "msauth")


def is_seeded():
    """True if the dedicated profile already has a signed-in-session marker."""
    for sub in ("Default/Secure Preferences", "Default/Network/Cookies",
                "Default/Cookies"):
        p = os.path.join(PROFILE_DIR, sub.replace("/", os.sep))
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return True
        except OSError:
            pass
    # Local Storage leveldb with bytes also counts (MSAL/token apps).
    base = os.path.join(PROFILE_DIR, "Default", "Local Storage", "leveldb")
    try:
        if os.path.isdir(base):
            for root, _d, files in os.walk(base):
                for fn in files:
                    if os.path.getsize(os.path.join(root, fn)) > 0:
                        return True
    except OSError:
        pass
    return False


def seed(src_profile="Default", include_storage=True):
    """Seed the dedicated profile with the minimal session files from the real
    Edge profile. Returns {ok, copied, seeded_from}. Idempotent-ish: always
    (re)copies the small floor; skips if the real profile is missing."""
    real_ud = _real_user_data()
    src = os.path.join(real_ud, src_profile)
    if not os.path.isdir(src):
        return {"ok": False, "error": f"real Edge profile not found: {src}"}
    os.makedirs(PROFILE_DIR, exist_ok=True)
    copied = 0
    # user-data-root Local State (DPAPI os_crypt key) — must come along.
    if _copy_one(os.path.join(real_ud, "Local State"),
                 os.path.join(PROFILE_DIR, "Local State")):
        copied += 1
    dst = os.path.join(PROFILE_DIR, "Default")
    for f in _SEED_PROFILE_FILES:
        if _copy_one(os.path.join(src, f), os.path.join(dst, f)):
            copied += 1
    if include_storage:
        for sub in _SEED_STORAGE_DIRS:
            sdir = os.path.join(src, sub)
            if os.path.isdir(sdir):
                copied += _copytree_live(sdir, os.path.join(dst, sub))
        # identity-origin IndexedDB only (auth stores; service caches skipped).
        idb = os.path.join(src, "IndexedDB")
        if os.path.isdir(idb):
            for entry in os.listdir(idb):
                if any(o in entry.lower() for o in _AUTH_ORIGINS):
                    s_e = os.path.join(idb, entry)
                    if os.path.isdir(s_e):
                        copied += _copytree_live(s_e, os.path.join(dst, "IndexedDB", entry))
    return {"ok": True, "copied": copied, "seeded_from": src}


# --- persistent headless Edge against the dedicated profile -----------------

_PROC = None
_PORT = None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _edge_alive_on(port):
    try:
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_running(headless=True):
    """Ensure a persistent Edge is up on the dedicated profile; return its debug
    URL (http://127.0.0.1:<port>) or None if Edge couldn't be launched.

    Seeds the profile on first use. Reuses an already-running instance (reads
    edge.port and probes it) so a daemon restart doesn't relaunch needlessly."""
    global _PROC, _PORT
    # Honor the headed latch even on a direct call (the Teams sync path needs it;
    # token/cookie reads work headed too).
    if headless and prefer_headed():
        headless = False
    # 1) reuse a live instance recorded in edge.port
    try:
        if os.path.isfile(PORT_FILE):
            p = int(open(PORT_FILE).read().strip())
            if _edge_alive_on(p):
                _PORT = p
                return f"http://127.0.0.1:{p}"
    except Exception:  # noqa: BLE001
        pass
    # 2) seed the profile if it's new — ONLY when explicitly enabled. Seeding
    #    (copying Secure Preferences + the refresh-token floor from the real Edge
    #    profile) produces a HALF-signed-in hybrid whose HMAC/account state does
    #    NOT actually sign in and does NOT sync (account_info stays empty). The
    #    reliable path is a CLEAN profile Edge builds itself + a one-time
    #    interactive sign-in (cdp.signin), which then persists and syncs like the
    #    old ai-pylon profile. Opt in with PYLON2_EDGE_SEED=1 only for the old
    #    best-effort behaviour.
    _seed_on = os.environ.get("PYLON2_EDGE_SEED", "").strip().lower() in ("1", "true", "yes")
    if _seed_on and not is_seeded():
        seed()
    edge = _edge_binary()
    if not edge:
        return None
    port = _free_port()
    # The managed profile is a purpose-built headless profile for the browser
    # capabilities — it should run NO third-party extensions. Disabling them
    # keeps foreign content scripts (and their console errors, e.g. a WXT/Plasmo
    # extension's "ko.runtime.connect is not a function" after its service worker
    # is invalidated) out of our profile, and avoids any extension interfering
    # with token/cookie extraction. Does NOT affect the user's normal Edge.
    # #14: keep the managed profile PLAIN-OWNED + trivially deletable. Edge's
    # component updater installs "component" dirs (CertificateRevocation,
    # WidevineCdm, EADPData, Edge * models, crx caches) under the user-data-dir
    # with TrustedInstaller/SYSTEM ownership — those survive a normal rmtree +
    # even takeown without elevation (the only thing the v1-profile delete this
    # session couldn't remove non-elevated). Disabling the component updater +
    # background component download at launch stops them accumulating, so the
    # profile stays deletable by a plain user.
    args = [edge, "--user-data-dir=%s" % PROFILE_DIR, "--profile-directory=Default",
            "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
            "--disable-extensions", "--disable-component-extensions-with-background-pages",
            "--disable-component-update",
            "--disable-features=msEdgeAutoLaunch,EdgeAutoLaunch,OptimizationGuideModelDownloading,"
            "OptimizationHints,MediaFoundationWidevineCdm,WidevineCdmComponent",
            "about:blank"]
    if headless:
        args.insert(1, "--headless=new")
    si = None
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0 if headless else 5  # SW_HIDE / SW_SHOW
    try:
        _PROC = subprocess.Popen(args, startupinfo=si,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return None
    # wait for the debug endpoint
    for _ in range(80):
        time.sleep(0.25)
        if _edge_alive_on(port):
            _PORT = port
            try:
                os.makedirs(os.path.dirname(PORT_FILE), exist_ok=True)
                with open(PORT_FILE, "w") as f:
                    f.write(str(port))
                with open(MODE_FILE, "w") as f:
                    f.write("headless" if headless else "headed")
            except OSError:
                pass
            return f"http://127.0.0.1:{port}"
    return None


def current_mode():
    """Return 'headless' | 'headed' | None for the live managed Edge (from the
    sidecar written by ensure_running). None if no instance is recorded."""
    try:
        if os.path.isfile(PORT_FILE) and os.path.isfile(MODE_FILE):
            p = int(open(PORT_FILE).read().strip())
            if _edge_alive_on(p):
                m = open(MODE_FILE).read().strip().lower()
                return m if m in ("headless", "headed") else None
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_mode(headless=True, timeout=40):
    """Ensure the managed Edge is running in the requested headed/headless mode.

    If a live instance already runs in the requested mode, reuse it (return its
    debug URL). If it runs in the OTHER mode, stop it and relaunch in the wanted
    mode. This is what the browser.fetch cap uses to force a NON-HEADLESS Edge
    for gateway hosts (which chrome-error under headless) without permanently
    changing the daemon's default headless token/cookie profile.

    Returns the debug URL (http://127.0.0.1:<port>) or None."""
    # Headed latch: once the Teams sync path prefers headed, a headless REQUEST
    # is downgraded to headed so the two caps don't fight over the single shared
    # instance (headless starves Teams' background sync → stale chat list). An
    # explicit headless=False always wins and SETS the latch.
    if not headless:
        set_prefer_headed(True)
    elif prefer_headed():
        headless = False
    want = "headless" if headless else "headed"
    have = current_mode()
    if have == want:
        try:
            p = int(open(PORT_FILE).read().strip())
            return f"http://127.0.0.1:{p}"
        except Exception:  # noqa: BLE001
            pass
    if have is not None:
        # running in the wrong mode — reap it so ensure_running relaunches fresh.
        stop()
        time.sleep(0.5)
    return ensure_running(headless=headless)


def _ps_quote(s):
    """Single-quote a string for a PowerShell literal (double any ' inside)."""
    return "'" + str(s).replace("'", "''") + "'"


def stop():
    """Stop the managed Edge and clear edge.port. Robust across daemon restarts:
    kills the Edge BOUND TO THIS PROFILE's --user-data-dir even when this process
    didn't launch it (reused instance) — matched by command line."""
    global _PROC
    # 1) the instance we launched, by PID tree.
    if _PROC is not None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(_PROC.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                _PROC.terminate()
        except Exception:  # noqa: BLE001
            pass
        _PROC = None
    # 2) any msedge bound to OUR profile dir (reused/orphaned instance). wmic is
    #    gone on recent Windows, so query Win32_Process via PowerShell CIM and
    #    match our unique --user-data-dir on the command line.
    if os.name == "nt":
        try:
            import subprocess as _sp
            ps = (
                "Get-CimInstance Win32_Process -Filter \"name='msedge.exe'\" | "
                "Where-Object { $_.CommandLine -like '*' + "
                + _ps_quote(PROFILE_DIR) + " + '*' } | "
                "ForEach-Object { $_.ProcessId }"
            )
            out = _sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                          capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                pid = line.strip()
                if pid.isdigit():
                    try:
                        _sp.run(["taskkill", "/F", "/T", "/PID", pid],
                                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
    # 3) forget the recorded port so the next start relaunches cleanly.
    try:
        if os.path.isfile(PORT_FILE):
            os.remove(PORT_FILE)
    except OSError:
        pass
    try:
        if os.path.isfile(MODE_FILE):
            os.remove(MODE_FILE)
    except OSError:
        pass
