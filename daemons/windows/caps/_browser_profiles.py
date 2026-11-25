"""Persistent browser profiles whose headless mode is independent of Teams/SSO."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from . import _cdp_common as common
from . import _edge_profile as edge

_LOCK = threading.RLock()


class Client:
    connect_ws = staticmethod(common.connect_ws)
    ws_send_recv = staticmethod(common.ws_send_recv)

    def __init__(self, profile, headful=None):
        if not re.fullmatch(r'[a-z0-9_-]{1,40}', profile):
            raise ValueError('Invalid browser profile name')
        self.profile = profile
        self.headful = headful
        with _LOCK:
            self.debug_url = self._ensure()

    def _json(self, path, method='GET'):
        request = urllib.request.Request(self.debug_url + path, method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def _ensure(self):
        folder = Path(edge._OWNER_DATA) / 'browser-profiles' / self.profile
        folder.mkdir(parents=True, exist_ok=True)
        state = folder / 'browser.json'
        try:
            record = json.loads(state.read_text(encoding='utf-8'))
            self.debug_url = 'http://127.0.0.1:' + str(int(record['port']))
            version = self._json('/json/version')
            if self.headful is None or record.get('headful') == self.headful:
                self._pid = record.get('pid')
                self._hide_window()
                return self.debug_url
            ws = common.connect_ws(version['webSocketDebuggerUrl'], timeout=5)
            try:
                common.ws_send_recv(ws, 'Browser.close', {}, timeout=5)
            except Exception:
                pass  # Browser.close normally disconnects its own socket.
            finally:
                ws.close()
            for _ in range(40):
                if not edge._edge_alive_on(record['port']):
                    break
                time.sleep(.1)
        except (OSError, ValueError, KeyError):
            pass
        binary = edge._edge_binary()
        if not binary:
            raise RuntimeError('Microsoft Edge was not found')
        port = edge._free_port()
        args = [binary, '--user-data-dir=' + str(folder / 'profile'), '--no-first-run',
                '--no-default-browser-check', '--disable-extensions', '--disable-component-update',
                '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + str(port),
                '--remote-allow-origins=*', '--window-size=1100,900', 'about:blank']
        if not self.headful:
            args.insert(1, '--headless=new')
        else:
            args[1:1] = ['--window-position=-32000,-32000', '--disable-backgrounding-occluded-windows',
                         '--disable-renderer-backgrounding', '--disable-background-timer-throttling']
        startup = None
        if os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        process = subprocess.Popen(args, startupinfo=startup, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            if edge._edge_alive_on(port):
                temporary = state.with_suffix('.tmp')
                temporary.write_text(json.dumps({'port': port, 'pid': process.pid, 'headful': bool(self.headful)}), encoding='utf-8')
                os.replace(temporary, state)
                self._pid = process.pid
                self._hide_window()
                return 'http://127.0.0.1:' + str(port)
            if process.poll() is not None:
                break
            time.sleep(.25)
        raise RuntimeError('Could not start the dedicated browser profile')

    def _hide_window(self):
        """Keep this profile's headed window hidden without activating any window."""
        if os.name != 'nt' or not getattr(self, '_pid', None):
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]

        @callback_type
        def hide(hwnd, unused):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self._pid:
                user32.ShowWindow(hwnd, 0)  # SW_HIDE never activates the browser.
            return True

        user32.EnumWindows(hide, 0)

    def ensure_available(self):
        return None

    def page_tabs(self):
        return [item for item in self._json('/json') if item.get('type') == 'page']

    def target_by_id(self, tab_id):
        return next((item for item in self.page_tabs() if item.get('id') == tab_id), None)

    def open_tab(self, url):
        tab = self._json('/json/new?' + urllib.parse.quote(url, safe=''), method='PUT')
        self._hide_window()
        return tab

    def close_tab(self, tab_id):
        if not tab_id:
            return
        try:
            with urllib.request.urlopen(self.debug_url + '/json/close/' + urllib.parse.quote(tab_id, safe=''), timeout=5):
                pass
        except OSError:
            pass

    def wait_for_ws(self, tab_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            target = self.target_by_id(tab_id)
            if target and target.get('webSocketDebuggerUrl'):
                return target
            time.sleep(.1)
        return None
