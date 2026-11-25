"""capability: cap:desktop.tray  (Windows, system notification area).

Endpoint-bound: a real system-tray icon for the ai-pylon2 platform. The icon
runs in a DEDICATED HELPER PROCESS (``_tray_host.py``) where pystray's
``Icon.run()`` owns the MAIN THREAD — which is what makes tray CLICKS work on
Windows (v1's tray is a standalone process for exactly this reason; running
pystray on a daemon worker thread painted the icon but dropped click messages).

This cap is the CONTROLLER: it spawns the host once and drives it over a tiny
local TCP channel (JSON per line) — set the icon colour to the worst active
STATE LEVEL (ok/pending/ongoing/bad), bump the unread badge, show a balloon,
update the tooltip. The notification HUB (system/notify) calls notify(); the hub
owns the store, this cap owns the icon.

Methods:
  status()                        -> {ok, running, level, unread}
  show()                          -> ensure the icon host is running; {ok,...}
  notify(title, message, level?, url?) -> balloon + bump badge/level; {ok}
  set_level(level)                -> set the icon colour; {ok, level}
  set_tooltip(text)               -> update the hover tooltip; {ok}
  set_menu(items)                 -> push controllable-app context-menu items
                                     [{label, op, confirm?}]; clicking POSTs the op
  clear()                         -> reset level 'ok' + unread 0; {ok}
  stop()                          -> quit the icon host; {ok}

Degrades to {ok:False, error} if pystray/Pillow are missing or the host can't
start (e.g. no interactive desktop).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST = os.path.join(_HERE, "_tray_host.py")
def _port_file():
    """Read when used, not at import: a daemon in server mode sets
    PYLON2_TRAY_PORT_FILE after the caps are imported (pylon-server), so it
    never drives another install's tray (e.g. the ai-pylon2 host on the same desktop)."""
    return (os.environ.get("PYLON2_TRAY_PORT_FILE")
            or os.path.join(os.environ.get("TEMP") or ".", "pylon2-tray.port"))

LEVELS = ("ok", "pending", "ongoing", "bad")
DEFAULT_LEVEL = "ok"
_KIND_LEVELS = {"alert": "bad", "crash": "bad", "error": "bad", "critical": "bad",
                "warning": "pending", "pending": "pending",
                "progress": "ongoing", "ongoing": "ongoing", "working": "ongoing"}


def _normalize_level(level, kind=""):
    c = str(level or "").strip().lower()
    if c in LEVELS:
        return c
    return _KIND_LEVELS.get(str(kind or "").strip().lower(), DEFAULT_LEVEL)


class _TrayController:
    """Spawns + drives the standalone tray host process (one per endpoint)."""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._fw_url = (os.environ.get("PYLON2_FRAMEWORK_HTTP")
                        or "http://127.0.0.1:8900")
        # #13: a persistent connection to the host so a burst of notifications
        # doesn't pay a TCP connect+teardown per toast. The host now reads
        # multiple newline-delimited frames per connection, so we keep one open
        # and reuse it under _send_lock; drop + reopen on any error.
        self._sock = None
        self._sock_port = 0
        self._send_lock = threading.Lock()

    # --- host lifecycle -----------------------------------------------------
    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def ensure(self):
        """Ensure the host process is running. Returns True if up/started."""
        with self._lock:
            if self._alive():
                return True
            if not os.path.isfile(_HOST):
                return False
            try:
                py = os.environ.get("PYLON2_PY") or sys.executable or "py"
                self._fw_url = (os.environ.get("PYLON2_FRAMEWORK_HTTP") or self._fw_url)
                args = [py, _HOST, self._fw_url]
                si = None
                creationflags = 0
                if os.name == "nt":
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0
                    # detached + no console so it's fully independent of the daemon.
                    creationflags = (0x00000008 | 0x00000200
                                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    os.remove(_port_file())
                except OSError:
                    pass
                self._proc = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, startupinfo=si,
                    creationflags=creationflags, close_fds=True)
                return True
            except Exception:  # noqa: BLE001
                self._proc = None
                return False

    def _port(self):
        try:
            return int(open(_port_file(), encoding="utf-8").read().strip())
        except Exception:  # noqa: BLE001
            return 0

    def _drop_sock(self):
        s, self._sock, self._sock_port = self._sock, None, 0
        if s is not None:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

    def _send_pooled(self, msg, port):
        """Try the persistent socket (reconnecting to `port` if needed); one
        request/reply frame. Raises on any socket error so the caller can fall
        back to the cold-start retry path (and we drop the dead socket)."""
        with self._send_lock:
            if self._sock is None or self._sock_port != port:
                self._drop_sock()
                self._sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                self._sock_port = port
            try:
                self._sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
                buf = b""
                self._sock.settimeout(3)
                while b"\n" not in buf:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("tray host closed the connection")
                    buf += chunk
                line = buf.split(b"\n", 1)[0]
                return json.loads(line.decode("utf-8", "replace").strip() or "{}")
            except Exception:
                self._drop_sock()
                raise

    def send(self, msg, *, start=True, wait_ready=True):
        """Send a control message (dict) to the host; return its reply dict or
        an error dict. Starts the host if needed and waits briefly for its
        control port on a cold start. Reuses a persistent socket on the warm
        path (#13); falls back to a fresh-connect retry loop on any error."""
        if start and not self.ensure():
            return {"ok": False, "error": "tray host unavailable (pystray/Pillow?)",
                    "error_category": "unavailable"}
        # fast path: a warm host with a known port — reuse the pooled socket.
        port = self._port()
        if port:
            try:
                return self._send_pooled(msg, port)
            except Exception:  # noqa: BLE001 — cold/host-restarted: fall through
                pass
        # slow path: cold start / port not yet published — fresh-connect retry.
        deadline = time.time() + (6 if wait_ready else 0.5)
        last = None
        while time.time() < deadline:
            port = self._port()
            if port:
                try:
                    return self._send_pooled(msg, port)
                except Exception as exc:  # noqa: BLE001 — host still warming
                    last = exc
            time.sleep(0.2)
        return {"ok": False, "error": "tray host not reachable%s"
                % (": %s" % last if last else ""), "error_category": "unavailable"}


_TRAY = _TrayController()


def _pystray_ok():
    try:
        import pystray  # noqa: F401
        import win32gui  # noqa: F401
        from PIL import Image  # noqa: F401
        return None
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "tray unavailable (pystray/Pillow/pywin32 missing: "
                f"{exc})", "error_category": "unavailable"}


# --- capability methods -----------------------------------------------------

def status(args, ctx):
    if not _TRAY._alive():
        return {"ok": True, "running": False}
    r = _TRAY.send({"cmd": "status"}, start=False, wait_ready=False)
    r.setdefault("running", bool(r.get("ok")))
    return r


def show(args, ctx):
    bad = _pystray_ok()
    if bad:
        return bad
    # ensure the host is up + confirm it answers status
    r = _TRAY.send({"cmd": "status"})
    if r.get("ok"):
        return {"ok": True, "running": True, "level": r.get("level", DEFAULT_LEVEL)}
    return r


def notify(args, ctx):
    bad = _pystray_ok()
    if bad:
        return bad
    message = (args.get("message") or args.get("msg") or "").strip()
    if not message:
        return {"ok": False, "error": "message required", "error_category": "application"}
    cmd = {"cmd": "notify", "title": (args.get("title") or os.environ.get("PYLON2_TRAY_NAME") or "ai-pylon2").strip(),
           "message": message,
           "level": _normalize_level(args.get("level"), args.get("kind", "")),
           "url": args.get("url", "")}
    # actionable + sticky toast passthrough (Windows-Update style): custom action
    # buttons + a 'reminder'/'urgent' scenario that stays until the user acts.
    if isinstance(args.get("buttons"), list):
        cmd["buttons"] = args["buttons"]
    if args.get("scenario"):
        cmd["scenario"] = str(args["scenario"])
    if args.get("tag"):
        cmd["tag"] = str(args["tag"])
    if args.get("open"):
        cmd["open"] = True
    return _TRAY.send(cmd)


def set_level(args, ctx):
    bad = _pystray_ok()
    if bad:
        return bad
    return _TRAY.send({"cmd": "level", "level": _normalize_level(args.get("level"),
                                                                args.get("kind", ""))})


def set_tooltip(args, ctx):
    bad = _pystray_ok()
    if bad:
        return bad
    return _TRAY.send({"cmd": "tooltip", "text": (args.get("text")
                       or args.get("tooltip") or os.environ.get("PYLON2_TRAY_NAME") or "ai-pylon2").strip()})


def clear(args, ctx):
    return _TRAY.send({"cmd": "clear"}, start=False, wait_ready=False)


def stop(args, ctx):
    if _TRAY._alive():
        _TRAY.send({"cmd": "quit"}, start=False, wait_ready=False)
        try:
            _TRAY._proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        _TRAY._proc = None
    _TRAY._drop_sock()   # #13: the host is gone — drop the pooled socket
    return {"ok": True, "running": False}


def set_menu(args, ctx):
    """Push the CONTROLLABLE-APP context-menu items (v1 tray-control revival).
    ``items`` = [{label, op, confirm?}] where op is a framework op path like
    'system/wakeup/pause'; clicking the item POSTs to that op from the host.
    Replaces the current dynamic items (pass [] to clear). The static items
    (Open notifications / dashboard / Clear / Quit) always remain."""
    bad = _pystray_ok()
    if bad:
        return bad
    items = args.get("items")
    if not isinstance(items, list):
        return {"ok": False, "error": "items (list of {label, op|url|children, confirm?}) required",
                "error_category": "application"}

    def _clean(lst, depth=0):
        out = []
        if depth > 6:
            return out          # guard against pathological nesting
        for it in (lst or []):
            if not isinstance(it, dict):
                continue
            kids = it.get("children")
            if isinstance(kids, list) and kids:
                sub = _clean(kids, depth + 1)
                if sub:
                    out.append({"label": str(it.get("label") or "?"), "children": sub})
                continue
            op = (it.get("op") or "").strip().strip("/")
            url = (it.get("url") or "").strip()
            if not (op or url):
                continue
            entry = {"label": str(it.get("label") or op or url), "confirm": bool(it.get("confirm"))}
            if op:
                entry["op"] = op
            if url:
                entry["url"] = url
            if isinstance(it.get("arg"), dict):
                entry["arg"] = it["arg"]
            out.append(entry)
        return out

    return _TRAY.send({"cmd": "set_menu", "items": _clean(items)})


METHODS = {"status": status, "show": show, "notify": notify, "set_level": set_level,
           "set_tooltip": set_tooltip, "clear": clear, "stop": stop, "set_menu": set_menu}
CAPABILITY = "cap:desktop.tray"
VARIANT = None
ATTRS = {"desktop": True}
