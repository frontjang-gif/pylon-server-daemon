"""Keep this thread's native menus opaque without changing Windows effects."""
import ctypes
from ctypes import wintypes

import win32api
import win32con
import win32gui


_HookProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                             wintypes.WPARAM, wintypes.LPARAM)
_SubclassProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                 wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                 ctypes.c_size_t, ctypes.c_size_t)


class _WindowPos(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('after', wintypes.HWND),
                ('x', ctypes.c_int), ('y', ctypes.c_int),
                ('cx', ctypes.c_int), ('cy', ctypes.c_int),
                ('flags', wintypes.UINT)]


class OpaquePopupMenus:
    """Scope a creation hook and native subclasses to one TrackPopupMenu call.

    Windows' fade path can leave a menu displaying its empty animation surface.
    Setting alpha to 255 after SWP_SHOWWINDOW gives it an opaque native surface.
    Doing this in HCBT_CREATEWND is too early (ERROR_INVALID_PARAMETER); a later
    timer introduces a visible delay. Windows still draws every menu item.
    """
    def __init__(self, *, on_error, on_applied):
        self.on_error = on_error
        self.on_applied = on_applied
        self.hook_handle = None
        self.windows = set()
        self.shown = set()
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.controls = ctypes.WinDLL('comctl32', use_last_error=True)
        self.user.SetWindowsHookExW.argtypes = [ctypes.c_int, _HookProc,
                                               wintypes.HINSTANCE, wintypes.DWORD]
        self.user.SetWindowsHookExW.restype = wintypes.HANDLE
        self.user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            wintypes.WPARAM, wintypes.LPARAM]
        self.user.CallNextHookEx.restype = ctypes.c_ssize_t
        self.user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        self.user.UnhookWindowsHookEx.restype = wintypes.BOOL
        self.user.SetLayeredWindowAttributes.argtypes = [wintypes.HWND,
                                                        wintypes.DWORD,
                                                        ctypes.c_ubyte,
                                                        wintypes.DWORD]
        self.user.SetLayeredWindowAttributes.restype = wintypes.BOOL
        self.controls.SetWindowSubclass.argtypes = [wintypes.HWND, _SubclassProc,
                                                    ctypes.c_size_t, ctypes.c_size_t]
        self.controls.SetWindowSubclass.restype = wintypes.BOOL
        self.controls.RemoveWindowSubclass.argtypes = [wintypes.HWND, _SubclassProc,
                                                       ctypes.c_size_t]
        self.controls.RemoveWindowSubclass.restype = wintypes.BOOL
        self.controls.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT,
                                                  wintypes.WPARAM, wintypes.LPARAM]
        self.controls.DefSubclassProc.restype = ctypes.c_ssize_t
        # Keep callbacks alive until every native reference has been removed.
        self._hook_callback = _HookProc(self._created)
        self._subclass_callback = _SubclassProc(self._message)

    def _created(self, code, wparam, lparam):
        try:
            if code == 3 and win32gui.GetClassName(wparam) == '#32768':
                if not self.controls.SetWindowSubclass(wparam, self._subclass_callback, 1, 0):
                    raise RuntimeError('SetWindowSubclass failed for native menu')
                self.windows.add(wparam)
        except Exception as exc:
            self.on_error(exc)
        return self.user.CallNextHookEx(self.hook_handle, code, wparam, lparam)

    def _message(self, hwnd, message, wparam, lparam, _identifier, _data):
        result = self.controls.DefSubclassProc(hwnd, message, wparam, lparam)
        try:
            if message == win32con.WM_WINDOWPOSCHANGED and hwnd not in self.shown:
                position = ctypes.cast(lparam, ctypes.POINTER(_WindowPos)).contents
                if position.flags & win32con.SWP_SHOWWINDOW:
                    self.shown.add(hwnd)
                    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE,
                                           style | win32con.WS_EX_LAYERED)
                    if not self.user.SetLayeredWindowAttributes(hwnd, 0, 255, 2):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self.on_applied()
            elif message == win32con.WM_NCDESTROY:
                self.controls.RemoveWindowSubclass(hwnd, self._subclass_callback, 1)
                self.windows.discard(hwnd)
                self.shown.discard(hwnd)
        except Exception as exc:
            self.on_error(exc)
        return result

    def __enter__(self):
        self.hook_handle = self.user.SetWindowsHookExW(
            5, self._hook_callback, None, win32api.GetCurrentThreadId())
        if not self.hook_handle:
            self.on_error(ctypes.WinError(ctypes.get_last_error()))
        return self

    def __exit__(self, *_exception):
        if self.hook_handle:
            self.user.UnhookWindowsHookEx(self.hook_handle)
            self.hook_handle = None
        for hwnd in tuple(self.windows):
            self.controls.RemoveWindowSubclass(hwnd, self._subclass_callback, 1)
        self.windows.clear()
        self.shown.clear()
