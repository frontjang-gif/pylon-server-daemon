"""_tray_pane — the notification floating pane as a NATIVE tkinter window.

NOT a capability (``_``-prefixed). The tray host spawns this (its own process,
tkinter owns the main-thread mainloop) to show a small, frameless, always-on-top
panel at the tray corner listing the notifications from the hub. This is v1's
"native panel fallback" approach — used here because WebView2/pywebview does not
initialize on this host, whereas tkinter (stdlib) works reliably.

It reads notifications from the framework over HTTP (the hub's
/v1/apps/system/notify/list), renders level-coloured cards, surfaces any device
sign-in code prominently (click to copy), and offers open-link / mark-read /
clear. Closes on × or Esc. One-shot: opens, user dismisses, process exits.

Usage: py -3 _tray_pane.py <framework_http_url>
"""
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # sibling helper modules
import _urlutil  # noqa: E402

_W, _H, _MARGIN = 400, 560, 12

_LEVEL = {"ok": "#4caf50", "pending": "#f5c518", "ongoing": "#2f81f7", "bad": "#d92d3a"}
_BG, _PANEL, _ROW, _BORDER, _TEXT, _MUTED = (
    "#171a1f", "#22272e", "#2b313a", "#424a55", "#f0f3f6", "#9da7b3")


def _api(fw, path, body=None):
    import urllib.request
    url = fw.rstrip("/") + path
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def _corner():
    """Bottom-right of the primary work area in LOGICAL coords (tkinter's space,
    DPI-unaware)."""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        r = wintypes.RECT()
        if u.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return max(r.left, r.right - _W - _MARGIN), max(r.top, r.bottom - _H - _MARGIN)
    except Exception:  # noqa: BLE001
        pass
    return 900, 400


def _extract_code(msg):
    m = re.search(r"code:?\s*([A-Z0-9]{6,})", msg or "") or re.search(r"enter\s+([A-Z0-9]{6,})", msg or "")
    return m.group(1) if m else ""


def open_link(url, fw):
    """Open a notification link. The stored link is page-relative (``/apps/..``): resolve it against the framework
    URL this pane talks to, or the OS has nothing to open ("missing server part")."""
    target = _urlutil.absolute(url, fw)
    if not target:
        return
    try:
        os.startfile(target)   # noqa: S606
    except Exception:  # noqa: BLE001
        import webbrowser
        webbrowser.open(target)


def _extract_url(n):
    if n.get("url"):
        return n["url"]
    m = re.search(r"(https?://[^\s]+)", n.get("message", "") or "")
    return m.group(1) if m else ""


def _log(msg):
    try:
        p = os.path.join(os.environ.get("TEMP") or ".", "pylon2-pane.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("pid=%s t=%.3f %s\n" % (os.getpid(), time.perf_counter(), msg))
    except Exception:  # noqa: BLE001
        pass


class ApiWorker:
    """One daemon worker; Tk only consumes completed results on its own loop."""
    def __init__(self, fw, api=None):
        self.fw = fw
        self.api = api or _api
        self.requests = queue.Queue()
        self.results = queue.Queue()
        self.closed = False
        threading.Thread(target=self._run, name='pane-http', daemon=True).start()

    def submit(self, token, operation='list', args=None):
        if not self.closed:
            self.requests.put((token, operation, args or {}))

    def close(self):
        self.closed = True
        self.requests.put(None)

    def _run(self):
        while True:
            request = self.requests.get()
            if request is None or self.closed:
                return
            token, operation, args = request
            try:
                result = self.api(self.fw, '/v1/apps/system/notify/' + operation, args)
                if result.get('ok') and operation != 'list':
                    result = self.api(self.fw, '/v1/apps/system/notify/list', {'limit': 100})
            except Exception as exc:
                result = {'ok': False, 'error': str(exc)}
            if not self.closed:
                self.results.put((token, result))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('framework', nargs='?', default='http://127.0.0.1:8900')
    parser.add_argument('--managed', action='store_true')
    parser.add_argument('--hidden', action='store_true')
    args = parser.parse_args()
    import tkinter as tk
    import traceback
    _log('main start')
    try:
        return _run(tk, args.framework, managed=args.managed, hidden=args.hidden)
    except Exception:
        _log('EXC:\n' + traceback.format_exc())
        return 1


def _run(tk, fw, *, managed=False, hidden=False):
    started = time.perf_counter()
    root = tk.Tk()
    root.withdraw()
    _log("Tk() ok")
    root.title((os.environ.get("PYLON2_TRAY_NAME") or "ai-pylon2") + " notifications")
    x, y = _corner()
    root.geometry("%dx%d+%d+%d" % (_W, _H, x, y))
    _log("geometry %dx%d+%d+%d" % (_W, _H, x, y))
    root.overrideredirect(True)                  # frameless
    root.attributes("-topmost", True)
    root.configure(bg=_BG)

    # header
    hdr = tk.Frame(root, bg=_PANEL, height=44)
    hdr.pack(fill="x", side="top")
    hdr.pack_propagate(False)
    tk.Label(hdr, text="Notifications", bg=_PANEL, fg=_TEXT,
             font=("Segoe UI", 11, "bold")).pack(side="left", padx=12)

    worker = ApiWorker(fw)
    commands = queue.Queue()
    state = {'closed': False, 'token': 0, 'pending': False, 'items': None,
             'render': 0, 'shown': 0}

    def _shutdown():
        if state['closed']:
            return
        state['closed'] = True
        _log('shutdown')
        worker.close()
        root.destroy()

    def _close(*_a):
        if managed:
            root.withdraw()
        else:
            _shutdown()
    tk.Button(hdr, text="✕", command=_close, bg=_PANEL, fg=_MUTED, bd=0,
              activebackground=_ROW, activeforeground=_TEXT,
              font=("Segoe UI", 12)).pack(side="right", padx=8)

    def _clear_all():
        _request('clear', {'confirm': True})
    clear_button = tk.Button(hdr, text="Clear", command=_clear_all, bg=_ROW, fg=_TEXT, bd=0,
              activebackground=_BORDER, font=("Segoe UI", 9))
    clear_button.pack(side="right", padx=4)

    def _read_all():
        _request('read', {'ids': '*', 'confirm': True})
    read_button = tk.Button(hdr, text="Mark read", command=_read_all, bg=_ROW, fg=_TEXT, bd=0,
              activebackground=_BORDER, font=("Segoe UI", 9))
    read_button.pack(side="right", padx=4)
    status = tk.Label(root, text='Loading notifications...', bg=_PANEL, fg=_MUTED,
                      anchor='w', font=('Segoe UI', 9), padx=10, pady=6)
    status.pack(side='bottom', fill='x')
    tk.Button(hdr, text='Refresh', command=lambda: _request(), bg=_ROW, fg=_TEXT, bd=0,
              activebackground=_BORDER, font=('Segoe UI', 9)).pack(side='right', padx=4)

    # scrollable body
    body = tk.Frame(root, bg=_BG)
    body.pack(fill="both", expand=True)
    canvas = tk.Canvas(body, bg=_BG, highlightthickness=0)
    sb = tk.Scrollbar(body, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=_BG)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw", width=_W - 16)
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

    def _copy(code, btn):
        try:
            root.clipboard_clear()
            root.clipboard_append(code)
            btn.config(text=code + "  ✓ copied")
        except Exception:  # noqa: BLE001
            pass

    def _open(url):
        open_link(url, fw)

    def _render(items):
        if items == state['items']:
            return
        state['items'] = items
        state['render'] += 1
        generation = state['render']
        for child in inner.winfo_children():
            child.destroy()
        canvas.yview_moveto(0)
        if not items:
            tk.Label(inner, text='No notifications.', bg=_BG, fg=_MUTED,
                     font=('Segoe UI', 10)).pack(pady=40)
            return
        more = tk.Button(inner, bg=_ROW, fg=_TEXT, bd=0, activebackground=_BORDER,
                         font=('Segoe UI', 9))
        def batch(index, end):
            if state['closed'] or generation != state['render']:
                return
            for notification in items[index:min(index + 4, end)]:
                _card(notification)
            if index + 4 < end:
                root.after(1, lambda: batch(index + 4, end))
            elif end < len(items):
                def next_page():
                    more.pack_forget()
                    batch(end, min(end + 12, len(items)))
                more.config(text='Show %s more (%s remaining)' % (
                    min(12, len(items) - end), len(items) - end), command=next_page)
                more.pack(fill='x', padx=8, pady=8)
        batch(0, min(12, len(items)))

    def _card(n):
        lvl = n.get("level", "ok")
        card = tk.Frame(inner, bg=_PANEL, highlightbackground=_LEVEL.get(lvl, _MUTED),
                        highlightthickness=0)
        card.pack(fill="x", padx=8, pady=5)
        strip = tk.Frame(card, bg=_LEVEL.get(lvl, _MUTED), width=4)
        strip.pack(side="left", fill="y")
        inner2 = tk.Frame(card, bg=_PANEL)
        inner2.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        tk.Label(inner2, text=n.get("title", ""), bg=_PANEL, fg=_TEXT,
                 font=("Segoe UI", 10, "bold"), anchor="w",
                 wraplength=_W - 60, justify="left").pack(fill="x")
        msg = n.get("message", "")
        if msg:
            tk.Label(inner2, text=msg, bg=_PANEL, fg="#d4dae1", anchor="w",
                     wraplength=_W - 60, justify="left",
                     font=("Segoe UI", 9)).pack(fill="x", pady=(2, 0))
        code = _extract_code(msg)
        if code:
            b = tk.Button(inner2, text=code, bg=_ROW, fg=_TEXT, bd=0,
                          font=("Consolas", 13, "bold"), activebackground=_BORDER)
            b.config(command=lambda c=code, bb=b: _copy(c, bb))
            b.pack(anchor="w", pady=(4, 0))
        url = _extract_url(n)
        if url:
            lk = tk.Label(inner2, text="Open link ↗", bg=_PANEL, fg="#2f81f7",
                          cursor="hand2", font=("Segoe UI", 9))
            lk.pack(anchor="w", pady=(4, 0))
            lk.bind("<Button-1>", lambda e, u=url: _open(u))
        meta = "%s · %s" % (n.get("source", ""), lvl)
        tk.Label(inner2, text=meta, bg=_PANEL, fg=_MUTED,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(4, 0))

    def _request(operation='list', args=None):
        if state['closed'] or (state['pending'] and operation == 'list'):
            return
        state['token'] += 1
        state['pending'] = True
        clear_button.config(state='disabled')
        read_button.config(state='disabled')
        status.config(text='Refreshing...' if state['items'] is not None else 'Loading notifications...')
        worker.submit(state['token'], operation, args or {'limit': 100})

    def _show():
        if state['closed']:
            return
        x, y = _corner()
        root.geometry('%dx%d+%d+%d' % (_W, _H, x, y))
        root.deiconify()
        root.lift()
        root.attributes('-topmost', True)
        try:
            root.focus_force()
        except Exception:
            pass
        state['shown'] += 1
        _log('shown count=%s elapsed_ms=%.1f cached=%s' % (
            state['shown'], (time.perf_counter() - started) * 1000, state['items'] is not None))
        root.after_idle(_request)

    def _drain():
        if state['closed']:
            return
        try:
            while True:
                command = commands.get_nowait()
                if command == 'quit':
                    _shutdown()
                    return
                if command == 'show':
                    _show()
        except queue.Empty:
            pass
        try:
            while True:
                token, result = worker.results.get_nowait()
                if token != state['token']:
                    continue
                state['pending'] = False
                clear_button.config(state='normal')
                read_button.config(state='normal')
                if result.get('ok'):
                    items = result.get('notifications') or []
                    _render(items)
                    _log('snapshot count=%s' % len(items))
                    status.config(text='%s notifications | %s unread' % (
                        len(items), result.get('unread', 0)))
                else:
                    status.config(text='Could not refresh: ' + str(result.get('error') or result.get('_error') or 'Unknown error'))
        except queue.Empty:
            pass
        root.after(25, _drain)

    def _control():
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line).get('cmd')
                except (ValueError, AttributeError):
                    continue
                if command in ('show', 'quit'):
                    commands.put(command)
        finally:
            # The tray parent exited or closed the private pipe.
            _log('control EOF')
            commands.put('quit')

    root.bind('<Escape>', _close)
    root.protocol('WM_DELETE_WINDOW', _close)
    if managed:
        threading.Thread(target=_control, name='pane-control', daemon=True).start()
    root.after(25, _drain)
    _log('ready elapsed_ms=%.1f hidden=%s' % ((time.perf_counter() - started) * 1000, hidden))
    if not hidden:
        root.after_idle(_show)
    try:
        root.mainloop()
    finally:
        worker.close()
    _log("mainloop returned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
