"""_tray_host — the system-tray icon as a STANDALONE process (v1's model).

NOT a capability (``_``-prefixed). The daemon's cap:desktop.tray spawns this and
drives it; the icon itself lives HERE, running ``pystray.Icon.run()`` on this
process's MAIN THREAD — which is the crucial difference from the earlier in-daemon
version. On Windows, pystray's tray message loop only reliably receives CLICKS
when run() owns the main thread (v1's tray.py is a dedicated process for exactly
this reason). Running it on a daemon worker thread painted the icon but dropped
clicks — the bug this fixes.

The daemon controls it over a tiny LOCAL TCP channel (one JSON line per command),
the same robust pattern as the flyout control socket:
    {"cmd":"notify","title":..,"message":..,"level":..,"url":..}
    {"cmd":"level","level":".."}  {"cmd":"state","unread":N}
    {"cmd":"tooltip","text":".."} {"cmd":"status"}  {"cmd":"quit"}
Replies one JSON line. The chosen port is written to <TEMP>/pylon2-tray.port.

Click handling (open the notification pane via os.startfile — verified to work)
lives here in the host, where the message loop delivers it.

Usage: py -3 _tray_host.py <framework_http_url>
"""
from __future__ import annotations

import copy
import json
import os
import re
import socket
import sys
import threading
import webbrowser

try:                                        # imported as a package module (tests, tooling)
    from . import _urlutil
except ImportError:                         # run as a top-level script (the normal way)
    import _urlutil

# pylon-server: one set of sidecar files PER INSTALL (PYLON2_TRAY_PORT_FILE, set
# by a daemon in server mode), so two installs on one desktop never share them.
_BASE = os.path.splitext(os.environ.get("PYLON2_TRAY_PORT_FILE")
                         or os.path.join(os.environ.get("TEMP") or ".", "pylon2-tray.port"))[0]
# The product this tray belongs to (pylon-server sets it in server mode).
NAME = os.environ.get("PYLON2_TRAY_NAME") or "ai-pylon2"
_PORT_FILE = _BASE + ".port"
_PID_FILE = _BASE + ".pid"
# Last-known tray aggregate (level + unread), so a restart restores it instead
# of flashing 'ok'. Small, in TEMP (same dir as the port/pid sidecars).
_STATE_FILE = _BASE + ".state"


def _kill_other_tray_hosts(fw=None):
    """SINGLETON enforcement: terminate any OTHER _tray_host.py process before we
    claim the tray. A fresh daemon start spawns a new host without a handle to a
    prior one (different daemon process), which left GHOST tray icons. This runs
    once at host startup: (1) kill the PID recorded by a previous host (fast,
    exact), then (2) sweep any remaining python running _tray_host.py (covers a
    missing/stale PID file). Never kills our own PID."""
    me = os.getpid()
    # (1) exact: the PID a previous host recorded.
    try:
        old = int(open(_PID_FILE, encoding="utf-8").read().strip())
        if old and old != me:
            _terminate(old)
    except Exception:  # noqa: BLE001
        pass
    # (2) sweep: any other python process whose command line runs _tray_host.py.
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe' or name='pythonw.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            if "_tray_host.py" not in line:
                continue
            # pylon-server: only hosts of THIS install (same framework URL argument);
            # another install's tray on the same desktop is never touched.
            if fw and fw.rstrip("/") not in [t.strip('"').rstrip("/") for t in line.replace(",", " ").split()]:
                continue
            for tok in line.replace(",", " ").split():
                if tok.isdigit():
                    pid = int(tok)
                    if pid != me:
                        _terminate(pid)
    except Exception:  # noqa: BLE001
        pass
    # record our own PID as the current singleton.
    try:
        with open(_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(me))
    except OSError:
        pass


def _terminate(pid):
    try:
        import subprocess
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=6,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        pass


def _extract_code(msg):
    """Pull a device/sign-in code out of a message ('... code: ABC123' / 'enter
    ABC123'), for the toast 'Copy code' button. '' if none."""
    m = (re.search(r"code:?\s*([A-Z0-9]{6,})", msg or "")
         or re.search(r"enter\s+([A-Z0-9]{6,})", msg or ""))
    return m.group(1) if m else ""

LEVELS = ("ok", "pending", "ongoing", "bad")
DEFAULT_LEVEL = "ok"
_SEVERITY = {lv: i for i, lv in enumerate(LEVELS)}
LEVEL_COLORS = {"ok": "#4caf50", "pending": "#f5c518",
                "ongoing": "#2f81f7", "bad": "#d92d3a"}


def _norm_level(level):
    c = str(level or "").strip().lower()
    return c if c in _SEVERITY else DEFAULT_LEVEL


class _ConnClosed(Exception):
    """Raised internally when a control-channel client closes its socket, to
    break out of the per-connection multi-frame read loop (#13)."""


class TrayHost:
    def __init__(self, fw_url):
        self.fw_url = (fw_url or "http://127.0.0.1:8900").rstrip("/")
        self.level = DEFAULT_LEVEL
        self.unread = 0
        self.tooltip = NAME
        self.pending_url = ""
        self._icon = None
        self._img_cache = {}          # (level, unread_bucket) -> PIL.Image
        # Dynamic "controllable apps" menu items pushed by the daemon via the
        # set_menu command (v1 tray-control revival): each is
        # {label, op, confirm?} where op is a framework op path like
        # 'system/wakeup/pause'. Clicking POSTs to fw_url + /v1/apps/<op>.
        self._pane_lock = threading.Lock()
        self._pane_proc = None
        self._menu_lock = threading.RLock()
        self._dyn_items = []
        self._restore_state()         # survive restarts: don't flash to 'ok'

    # --- persisted level/unread (survive a daemon/tray restart) ------------
    def _restore_state(self):
        """Read the last persisted (level, unread) so a restart shows the real
        aggregate immediately instead of flashing 'ok' then re-colouring. Purely
        cosmetic + avoids a needless icon re-render on the next notify."""
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            self.level = _norm_level(st.get("level"))
            self.unread = max(0, int(st.get("unread", 0)))
        except Exception:  # noqa: BLE001
            pass

    def _persist_state(self):
        try:
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"level": self.level, "unread": self.unread}, f)
        except Exception:  # noqa: BLE001
            pass

    # --- icon image (brand "P" disc + unread badge) ------------------------
    def _image(self):
        """Rendered tray icon for the current (level, unread) — CACHED. The disc+
        glyph+badge render is pure in (level, unread-bucket); only a handful of
        combinations ever occur (4 levels × {0,1..9,9+}), so cache by that key
        and avoid re-rendering on every _refresh (level/badge change)."""
        bucket = 0 if self.unread <= 0 else (self.unread if self.unread <= 9 else 10)
        key = (self.level, bucket)
        img = self._img_cache.get(key)
        if img is None:
            img = self._render_image()
            self._img_cache[key] = img
        return img

    def _render_image(self):
        from PIL import Image, ImageDraw, ImageFont
        color = LEVEL_COLORS.get(self.level, LEVEL_COLORS[DEFAULT_LEVEL])
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        m = 4
        d.ellipse([m, m, size - 1 - m, size - 1 - m], fill=color)
        font = None
        for name in ("arialbd.ttf", "segoeuib.ttf", "seguisb.ttf"):
            try:
                font = ImageFont.truetype(name, 40)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
        tb = d.textbbox((0, 0), "P", font=font)
        d.text((size / 2 - (tb[2] - tb[0]) / 2 - tb[0], size / 2 - (tb[3] - tb[1]) / 2 - tb[1]),
               "P", fill="white", font=font)
        if self.unread > 0:
            r = 22
            bx = [size - r - 2, 0, size - 2, r]
            d.ellipse(bx, fill="#d92d3a", outline="white", width=2)
            btxt = "9+" if self.unread > 9 else str(self.unread)
            bf = font.font_variant(size=16) if hasattr(font, "font_variant") else font
            tb2 = d.textbbox((0, 0), btxt, font=bf)
            d.text(((bx[0] + bx[2]) / 2 - (tb2[2] - tb2[0]) / 2 - tb2[0],
                    (bx[1] + bx[3]) / 2 - (tb2[3] - tb2[1]) / 2 - tb2[1]),
                   btxt, fill="white", font=bf)
        return img

    # --- menu + click actions (run where the message loop delivers them) ---
    def _menu(self):
        import pystray
        items = [
            pystray.MenuItem(NAME, lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open notifications", self._click(self._do_open_notifications),
                             default=True),
            pystray.MenuItem("Open " + NAME, self._click(self._do_open_dashboard)),
            pystray.MenuItem("Clear status", self._click(self._do_clear)),
        ]
        # v1 revival: controllable-app items pushed via set_menu. Supports nested
        # submenus (children) mirroring v1's apps/pages folder tree; a leaf has
        # op (POST) or url (open). Bind each item via a default-arg so the loop
        # var isn't captured by reference.
        if self._dyn_items:
            items.append(pystray.Menu.SEPARATOR)
            items.extend(self._render_items(self._dyn_items))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Quit", self._quit))
        return pystray.Menu(*items)

    def _render_items(self, defs):
        """Recursively turn [{label, children|op|url, confirm?}] into pystray
        MenuItems. A `children` list -> a nested submenu (v1 folder nesting); an
        op/url -> a clickable leaf."""
        import pystray
        out = []
        for it in (defs or []):
            if not isinstance(it, dict):
                continue
            label = str(it.get("label") or it.get("op") or it.get("url") or "?")
            kids = it.get("children")
            if isinstance(kids, list) and kids:
                sub = self._render_items(kids)
                if sub:
                    out.append(pystray.MenuItem(label, pystray.Menu(*sub)))
            elif it.get("op") or it.get("url"):
                out.append(pystray.MenuItem(
                    label, self._click(lambda _i=it: self._do_invoke(_i))))
        return out

    def _fw_is_local(self):
        """#5: is the framework URL a loopback address? The tray op back-channel
        POSTs with no auth beyond the item's confirm flag, so it is only safe
        against a localhost framework. A non-loopback host means a remote
        endpoint tray, which must add real auth before firing ops — refuse until
        then. Resolves the hostname so '127.0.0.1', 'localhost', and '::1' all
        pass while a real remote host does not."""
        try:
            from urllib.parse import urlparse
            host = (urlparse(self.fw_url).hostname or "").strip().lower()
        except Exception:  # noqa: BLE001
            return False
        if host in ("127.0.0.1", "localhost", "::1", ""):
            return True
        # resolve: accept only if every resolved address is loopback
        try:
            import socket
            infos = socket.getaddrinfo(host, None)
            addrs = {ai[4][0] for ai in infos}
            return bool(addrs) and all(
                a == "::1" or a.startswith("127.") for a in addrs)
        except Exception:  # noqa: BLE001
            return False

    def _do_invoke(self, item):
        """A controllable-app menu click. Two item kinds:
          {label, url}  -> OPEN the url (navigate to the app's page) in the browser
          {label, op}   -> POST the framework op (e.g. system/wakeup/pause)
        Runs on a worker thread (via _click). Best-effort; balloon on failure."""
        url = str(item.get("url") or "").strip()
        if url:
            # app-page open: absolute, or relative to the framework URL
            target = url if "://" in url else (self.fw_url + "/" + url.lstrip("/"))
            self._open_url(target)
            return
        op = str(item.get("op") or "").strip().strip("/")
        if not op:
            return
        # #5: the op back-channel is TRUSTED-LOCALHOST-ONLY. A tray click POSTs
        # the op with no auth beyond the item's confirm flag, so it must only
        # ever reach a loopback framework. Refuse to fire an op at a non-loopback
        # fw_url (a future remote-endpoint tray must add real auth before this).
        if not self._fw_is_local():
            try:
                self._icon.notify("blocked %s: tray ops are localhost-only" % op, NAME)
            except Exception:  # noqa: BLE001
                pass
            return
        op_url = "%s/v1/apps/%s" % (self.fw_url, op)
        # body: an explicit `arg` dict (e.g. {app, enabled}) merged with confirm;
        # else just confirm when requested.
        body = {}
        if isinstance(item.get("arg"), dict):
            body.update(item["arg"])
        if item.get("confirm"):
            body["confirm"] = True
        try:
            import urllib.request
            req = urllib.request.Request(
                op_url, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
            if isinstance(result, dict) and result.get("ok") is False:
                raise RuntimeError(result.get("error") or result.get("_error") or "operation failed")
            if isinstance(result, dict) and result.get("warning"):
                self._icon.notify(str(result["warning"]), NAME)
        except Exception as exc:  # noqa: BLE001
            try:
                self._icon.notify("could not run %s: %s" % (op, str(exc)[:80]), NAME)
            except Exception:  # noqa: BLE001
                pass

    def _click(self, fn):
        """Wrap a click action so it runs on a SHORT-LIVED THREAD — the pystray
        message loop (main thread) must return immediately or the tray icon
        FREEZES (clicks/menu stop responding). This is the fix for the frozen
        icon: no click handler does blocking work (subprocess spawn, terminate,
        os.startfile) on the loop thread."""
        def _handler(*_a):
            threading.Thread(target=fn, daemon=True).start()
        return _handler

    def _open_url(self, target):
        # a page-relative link ("/apps/...") has no server part: resolve it against the framework URL we dial
        target = _urlutil.absolute(target, self.fw_url)
        if not target:
            return
        # os.startfile is the reliable default-browser launch on Windows
        # (verified working from this session context); fall back to webbrowser.
        try:
            os.startfile(target)   # noqa: S606 — our own localhost URL
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            webbrowser.open(target)
        except Exception:  # noqa: BLE001
            pass

    def _ensure_pane(self, *, hidden=False):
        """Start once, then show the warm pane through its private stdin pipe."""
        import subprocess
        pane = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tray_pane.py')
        if not os.path.isfile(pane):
            return False
        with self._pane_lock:
            previous = self._pane_proc
            if previous is not None and previous.poll() is None:
                if hidden:
                    return True
                try:
                    previous.stdin.write(json.dumps({'cmd': 'show'}) + '\n')
                    previous.stdin.flush()
                    return True
                except (OSError, ValueError, AttributeError):
                    try:
                        previous.terminate()
                        previous.wait(timeout=1)
                    except Exception:
                        pass
            try:
                py = sys.executable or 'py'
                pyw = py.lower().replace('python.exe', 'pythonw.exe')
                if pyw.endswith('pythonw.exe') and os.path.isfile(pyw):
                    py = pyw
                args = [py, pane, self.fw_url, '--managed']
                if hidden:
                    args.append('--hidden')
                # pythonw hides the console; do not hide the Tk window itself.
                self._pane_proc = subprocess.Popen(
                    args, stdin=subprocess.PIPE, text=True, encoding='utf-8',
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                return True
            except Exception:
                self._pane_proc = None
                return False

    def _stop_pane(self):
        with self._pane_lock:
            proc, self._pane_proc = self._pane_proc, None
            if proc is None:
                return
            try:
                proc.stdin.close()  # EOF closes the child on its Tk loop.
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def _open_pane(self, target_url):
        if not self._ensure_pane():
            self._open_url(target_url)

    def _do_open_notifications(self):
        # Tray-icon click ALWAYS shows the notification pane. It does NOT hijack
        # to a pending URL — the actionable URL (e.g. a device-auth sign-in) is
        # handled by the TOAST's "Open" button, not by clicking the tray icon.
        self._open_pane(self.fw_url + "/apps/system/notify/")

    def _do_open_dashboard(self):
        self._open_url(self.fw_url + "/")

    def _do_clear(self):
        self.level = DEFAULT_LEVEL
        self.unread = 0
        self.pending_url = ""
        self._refresh()

    def _quit(self, *_a):
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001
            pass

    # --- apply state to the live icon --------------------------------------
    def _refresh(self):
        self._persist_state()         # record level/unread for restart restore
        if self._icon is not None:
            try:
                self._icon.icon = self._image()
                self._icon.title = self.tooltip
            except Exception:  # noqa: BLE001
                pass

    def apply(self, msg):
        cmd = (msg.get("cmd") or "").lower()
        if cmd == "notify":
            lvl = _norm_level(msg.get("level"))
            self.unread += 1
            if _SEVERITY.get(lvl, 0) >= _SEVERITY.get(self.level, 0):
                self.level = lvl
            url = str(msg.get("url") or "")
            title = str(msg.get("title", NAME))
            body = str(msg.get("message", ""))
            if url:
                self.pending_url = url
            self._refresh()
            # Prefer a REAL Windows toast with buttons for actionable
            # notifications; fall back to pystray's (button-less, non-clickable)
            # balloon when windows-toasts isn't available. buttons/scenario come
            # from the notification type (system/notify) — custom action buttons
            # (Windows-Update style) + a sticky 'reminder'/'urgent' scenario.
            shown = False
            toast_err = ""
            code = _extract_code(body)
            buttons = msg.get("buttons") if isinstance(msg.get("buttons"), list) else None
            scenario = str(msg.get("scenario") or "")
            tag = str(msg.get("tag") or "")
            if url or code or buttons or scenario:
                try:
                    import _toast   # sibling module (host runs as a top-level script)
                    if _toast.available():
                        shown = _toast.show(title, body, url=url, code=code,
                                            buttons=buttons, scenario=scenario,
                                            fw_url=self.fw_url, tag=tag)
                        if not shown:
                            toast_err = "show returned False"
                except Exception as _te:  # noqa: BLE001
                    toast_err = repr(_te)
            if not shown:
                try:
                    self._icon.notify(body, title)
                except Exception:  # noqa: BLE001
                    pass
            if url and msg.get("open") and not shown:
                threading.Thread(target=self._open_url, args=(url,), daemon=True).start()
            return {"ok": True, "level": self.level, "unread": self.unread,
                    "toast": shown, "toast_err": toast_err}
        if cmd == "level":
            self.level = _norm_level(msg.get("level"))
            self._refresh()
            return {"ok": True, "level": self.level}
        if cmd == "state":
            if "unread" in msg:
                self.unread = max(0, int(msg["unread"]))
            if "level" in msg:
                self.level = _norm_level(msg["level"])
            self._refresh()
            return {"ok": True, "level": self.level, "unread": self.unread}
        if cmd == "tooltip":
            self.tooltip = str(msg.get("text") or NAME)[:120]
            self._refresh()
            return {"ok": True}
        if cmd == "set_menu":
            items = msg.get("items")
            if not isinstance(items, list):
                return {"ok": False, "error": "items must be a list"}
            snapshot = copy.deepcopy([i for i in items
                if isinstance(i, dict) and (i.get("op") or i.get("url") or i.get("children"))])
            with self._menu_lock:
                if snapshot == self._dyn_items:
                    return {"ok": True, "items": len(snapshot), "unchanged": True}
                previous = self._dyn_items
                self._dyn_items = snapshot
                try:
                    if self._icon is not None:
                        # The setter requests one UI-thread update. Calling
                        # update_menu again would rebuild the same menu twice.
                        self._icon.menu = self._menu()
                except Exception as exc:
                    self._dyn_items = previous
                    return {"ok": False, "error": str(exc)}
            return {"ok": True, "items": len(snapshot)}
        if cmd == "clear":
            self._do_clear()
            return {"ok": True}
        if cmd == "status":
            pane = self._pane_proc
            return {"ok": True, "running": True, "level": self.level,
                    "unread": self.unread, "tooltip": self.tooltip,
                    "menu_items": len(self._dyn_items),
                    "menu_tracking": bool(getattr(self._icon, '_menu_tracking', False)),
                    "menu_paints": getattr(self._icon, '_menu_paints', 0),
                    "menu_renderer": getattr(self._icon, 'menu_renderer', 'native'),
                    "popup_paints": getattr(self._icon, '_popup_paints', 0),
                    "menu_opacity_updates": getattr(self._icon, '_menu_opacity_updates', 0),
                    "menu_error": getattr(self._icon, '_last_menu_error', ''),
                    "pane_running": pane is not None and pane.poll() is None,
                    "pane_pid": getattr(pane, 'pid', None)}
        if cmd == "quit":
            threading.Timer(0.1, self._quit).start()
            return {"ok": True, "quitting": True}
        return {"ok": False, "error": "unknown cmd %r" % cmd}

    # --- TCP control server (daemon → host) --------------------------------
    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]
        try:
            with open(_PORT_FILE, "w", encoding="utf-8") as f:
                f.write(str(port))
        except OSError:
            pass
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:  # noqa: BLE001
                return
            # #13: handle MULTIPLE newline-delimited frames per connection so the
            # controller can keep a persistent socket and a burst of toasts pays
            # one connect, not one per message. We read frames until the client
            # closes (recv returns b''). Single-threaded control channel: a
            # second concurrent client simply queues at accept(), which is fine
            # for this low-traffic icon channel.
            try:
                conn.settimeout(300)   # idle-close a stale persistent client
                buf = b""
                while True:
                    while b"\n" not in buf:
                        if len(buf) >= 65536:
                            buf = b""   # overlong frame w/o newline: drop + resync
                        chunk = conn.recv(1024)
                        if not chunk:
                            raise _ConnClosed()
                        buf += chunk
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8", "replace").strip() or "{}")
                    except ValueError:
                        msg = {}
                    reply = self.apply(msg) if isinstance(msg, dict) else {"ok": False}
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except Exception:  # noqa: BLE001 — client closed / idle timeout / error
                pass
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def _self_populate_menu(self):
        """On (re)start, PULL the per-app menu from the framework so the context
        menu is populated automatically — not left empty until something happens
        to push it. Calls system/tray.menu (a read-only snapshot),
        then applies the items locally. Best-effort + bounded retries (the
        framework/notify may still be warming when the daemon respawns the host).
        Runs on a background thread so it never blocks the pystray loop."""
        import json as _json
        import time as _time
        import urllib.request
        url = self.fw_url + "/v1/apps/system/tray/menu"
        # #10: the FIRST attempt is immediate (no leading sleep) so a warm
        # framework populates the menu instantly; subsequent retries back off
        # (1s, 2s, then 3s) to catch a framework that's still warming after a
        # daemon respawn without a 3s dead window up front.
        _backoff = [1, 2, 3, 3, 3, 3, 3, 3, 3]
        for _attempt in range(10):
            try:
                req = urllib.request.Request(url, data=b"{}",
                                             headers={"Content-Type": "application/json"},
                                             method="POST")
                raw = urllib.request.urlopen(req, timeout=8).read()
                data = _json.loads(raw.decode("utf-8", "replace") or "{}")
                items = data.get("assembled")
                if isinstance(items, list):
                    # apply via the same path as a set_menu command
                    self.apply({"cmd": "set_menu", "items": items})
                    return
            except Exception:  # noqa: BLE001 — framework still warming; retry
                pass
            if _attempt < len(_backoff):
                _time.sleep(_backoff[_attempt])

    def run(self):
        # Pre-warm the Windows toaster in the background so the FIRST actionable
        # toast (often the sign-in prompt) isn't slowed by WinRT cold init.
        try:
            import _toast
            _toast.prewarm()
        except Exception:  # noqa: BLE001
            pass
        from _tray_icon import icon_class
        self._icon = icon_class()(NAME, self._image(), self.tooltip, menu=self._menu())
        threading.Thread(target=lambda: self._ensure_pane(hidden=True),
                         name="tray-pane-warm", daemon=True).start()
        # Construct the icon before workers can publish menu snapshots.
        # Native menu construction is marshalled to its UI message loop.
        threading.Thread(target=self._serve, name="tray-ctl", daemon=True).start()
        # self-populate the per-app context menu from the framework so it's there
        # automatically on every (re)start — not empty until a push (the "menu
        # gone after a daemon restart" fix).
        threading.Thread(target=self._self_populate_menu, name="tray-menu-populate",
                         daemon=True).start()
        try:
            self._icon.run()                  # main thread
        finally:
            self._stop_pane()
        for f in (_PORT_FILE, _PID_FILE):
            try:
                os.remove(f)
            except OSError:
                pass


def main():
    fw = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8900"
    _kill_other_tray_hosts(fw)    # singleton per install: never leave ghost tray icons
    TrayHost(fw).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
