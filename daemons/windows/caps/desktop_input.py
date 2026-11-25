"""capability: cap:desktop.input  (OS-level keyboard/mouse input simulation).

Endpoint-bound: injects genuine physical input events via pynput, so a browser
(or any app) receives them as real OS input. Ported from ai-pylon
apps/system/ui_input/server.py (the pynput simulation core). The app
apps/system/ui_input is a thin front that calls this cap — the pynput import +
the physical side effect live HERE, on the endpoint, never in the app.

Methods:
  status()                                    -> {ok, available}
  type({text})                                -> {ok, chars}      (physical keypresses)
  key({key, ctrl?, shift?, alt?})             -> {ok, key}        (single key + modifiers)
  mouse({action, x?, y?, button?})            -> {ok, action}     (click/double_click/move/scroll)

Degrades to {ok:False, error_category:unavailable} when pynput isn't importable.
"""
from __future__ import annotations

import time

_kb = None
_mouse = None


def _keyboard():
    global _kb
    if _kb is None:
        from pynput.keyboard import Controller
        _kb = Controller()
    return _kb


def _mouse_ctl():
    global _mouse
    if _mouse is None:
        from pynput.mouse import Controller
        _mouse = Controller()
    return _mouse


def _available():
    try:
        import pynput  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _na():
    return {"ok": False, "error": "pynput not available on this endpoint",
            "error_category": "unavailable"}


def status(args, ctx):
    return {"ok": True, "available": _available()}


def type(args, ctx):  # noqa: A001 — matches the v1 method name
    if not _available():
        return _na()
    text = (args or {}).get("text", "") or ""
    try:
        from pynput.keyboard import Key
        kb = _keyboard()
        for ch in text:
            if ch in ("\r", "\n"):
                kb.press(Key.enter); kb.release(Key.enter)
            elif ch == "\t":
                kb.press(Key.tab); kb.release(Key.tab)
            elif ch in ("\x7f", "\b"):
                kb.press(Key.backspace); kb.release(Key.backspace)
            elif ch == "\x1b":
                kb.press(Key.esc); kb.release(Key.esc)
            else:
                kb.type(ch)
            time.sleep(0.01)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    return {"ok": True, "chars": len(text)}


_SPECIAL = None


def _special_map():
    global _SPECIAL
    if _SPECIAL is None:
        from pynput.keyboard import Key
        _SPECIAL = {
            "enter": Key.enter, "return": Key.enter, "tab": Key.tab,
            "escape": Key.esc, "esc": Key.esc, "backspace": Key.backspace,
            "delete": Key.delete, "up": Key.up, "down": Key.down, "left": Key.left,
            "right": Key.right, "home": Key.home, "end": Key.end,
            "pageup": Key.page_up, "pagedown": Key.page_down, "space": Key.space,
            "insert": Key.insert,
            **{"f%d" % i: getattr(Key, "f%d" % i) for i in range(1, 13)},
        }
    return _SPECIAL


def key(args, ctx):
    if not _available():
        return _na()
    a = args or {}
    k = (a.get("key") or "")
    if not k:
        return {"ok": False, "error": "key required"}
    try:
        from pynput.keyboard import Key
        kb = _keyboard()
        mods = []
        if a.get("ctrl"):
            mods.append(Key.ctrl_l)
        if a.get("shift"):
            mods.append(Key.shift_l)
        if a.get("alt"):
            mods.append(Key.alt_l)
        for m in mods:
            kb.press(m)
        special = _special_map().get(k.lower())
        if special:
            kb.press(special); kb.release(special)
        elif len(k) == 1:
            kb.press(k); kb.release(k)
        for m in reversed(mods):
            kb.release(m)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    return {"ok": True, "key": k}


def mouse(args, ctx):
    if not _available():
        return _na()
    a = args or {}
    action = a.get("action", "click")
    try:
        from pynput.mouse import Button
        m = _mouse_ctl()
        btn = {"left": Button.left, "right": Button.right,
               "middle": Button.middle}.get(a.get("button", "left"), Button.left)
        x, y = a.get("x"), a.get("y")
        if x is not None and y is not None:
            m.position = (int(x), int(y))
        if action == "click":
            m.click(btn)
        elif action == "double_click":
            m.click(btn, 2)
        elif action == "scroll":
            m.scroll(0, int(y or 0))
        # 'move' already applied via position above
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}
    return {"ok": True, "action": action}


def window_rect(args, ctx):
    """GetWindowRect(hwnd) -> {ok, rect:[left,top,right,bottom]} (screen px).
    Windows-only (ctypes user32). Used by framework.os_automation to map a page
    viewport point to an absolute screen point for OS mouse input (#1)."""
    a = args or {}
    try:
        hwnd = int(a.get("hwnd") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "hwnd (int) required", "error_category": "application"}
    if not hwnd:
        return {"ok": False, "error": "hwnd (int) required", "error_category": "application"}
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        r = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return {"ok": False, "error": "GetWindowRect failed", "error_category": "application"}
        return {"ok": True, "rect": [r.left, r.top, r.right, r.bottom]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}


def foreground(args, ctx):
    """Force ``hwnd`` to the foreground via the AttachThreadInput recipe — the
    #92/#29 probes proved SetForegroundWindow ALONE is blocked by Windows'
    foreground lock, so we attach our thread input to the foreground window's
    thread first, then SetForegroundWindow succeeds. Windows-only (ctypes).
    Returns {ok, foreground:bool} (whether the target is foreground afterward)."""
    a = args or {}
    try:
        hwnd = int(a.get("hwnd") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "hwnd (int) required", "error_category": "application"}
    if not hwnd:
        return {"ok": False, "error": "hwnd (int) required", "error_category": "application"}
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        target = wintypes.HWND(hwnd)
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tgt_tid = user32.GetWindowThreadProcessId(target, None)
        # attach our (and the current foreground's) thread input to the target's
        # so SetForegroundWindow is permitted, then detach.
        attached = []
        try:
            if fg_tid and fg_tid != tgt_tid:
                user32.AttachThreadInput(fg_tid, tgt_tid, True); attached.append((fg_tid, tgt_tid))
            if cur_tid and cur_tid != tgt_tid:
                user32.AttachThreadInput(cur_tid, tgt_tid, True); attached.append((cur_tid, tgt_tid))
            SW_RESTORE = 9
            user32.ShowWindow(target, SW_RESTORE)
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
        finally:
            for a1, a2 in attached:
                try:
                    user32.AttachThreadInput(a1, a2, False)
                except Exception:  # noqa: BLE001
                    pass
        now_fg = user32.GetForegroundWindow()
        return {"ok": True, "foreground": bool(now_fg == hwnd)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}


def window_from_pid(args, ctx):
    """Find the top-level VISIBLE window owned by ``pid`` OR any of its descendant
    processes, returning {ok, hwnd, rect, title}. Edge (and other multi-process
    browsers) spawn the actual window in a CHILD process, so matching the launch
    PID alone misses it — we walk the process tree (via toolhelp snapshot) to
    collect the PID + its descendants, then EnumWindows for the biggest visible
    top-level window owned by any of them. Windows-only (ctypes). Used to wire
    sap_cats' window_hwnd for the os_input set_cell strategy (#92)."""
    a = args or {}
    try:
        root_pid = int(a.get("pid") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "pid (int) required", "error_category": "application"}
    if not root_pid:
        return {"ok": False, "error": "pid (int) required", "error_category": "application"}
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 1) build the descendant-PID set via a toolhelp process snapshot.
        TH32CS_SNAPPROCESS = 0x2

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
        parent_of = {}
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap and snap != -1:
            pe = PROCESSENTRY32(); pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snap, ctypes.byref(pe))
            while ok:
                parent_of[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
                ok = kernel32.Process32Next(snap, ctypes.byref(pe))
            kernel32.CloseHandle(snap)
        pids = {root_pid}
        # expand to descendants (bounded passes — process trees are shallow)
        for _ in range(8):
            added = False
            for cpid, ppid in parent_of.items():
                if ppid in pids and cpid not in pids:
                    pids.add(cpid); added = True
            if not added:
                break

        # 2) EnumWindows for the biggest visible top-level window owned by a pid.
        best = {"hwnd": 0, "area": -1, "rect": None, "title": ""}
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if int(wpid.value) not in pids:
                return True
            r = wintypes.RECT()
            if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
                return True
            area = (r.right - r.left) * (r.bottom - r.top)
            if area <= best["area"]:
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            best.update(hwnd=int(hwnd), area=area,
                        rect=[r.left, r.top, r.right, r.bottom], title=buf.value)
            return True
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if not best["hwnd"]:
            return {"ok": False, "error": "no visible top-level window for pid %d "
                    "(or its children) — is the browser headless?" % root_pid,
                    "error_category": "unavailable"}
        return {"ok": True, "hwnd": best["hwnd"], "rect": best["rect"], "title": best["title"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}


METHODS = {"status": status, "type": type, "key": key, "mouse": mouse,
           "window_rect": window_rect, "foreground": foreground,
           "window_from_pid": window_from_pid}
CAPABILITY = "cap:desktop.input"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
