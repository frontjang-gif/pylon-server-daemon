"""Keep native menu handles owned by the tray loop, including popup tracking."""
import threading


class MenuUpdateGuard:
    """Mixin: workers request updates; only the UI loop rebuilds native menus."""
    def __init__(self, *args, **kwargs):
        self._menu_update_lock = threading.Lock()
        self._menu_update_pending = False
        self._menu_tracking = False
        super().__init__(*args, **kwargs)

    def _update_menu(self):
        with self._menu_update_lock:
            self._menu_update_pending = True
        self._post_menu_update()

    def _flush_menu_update(self, *_args):
        if self._menu_tracking:
            return
        with self._menu_update_lock:
            if not self._menu_update_pending:
                return
            self._menu_update_pending = False
        super()._update_menu()

    def _track_popup(self, callback):
        if self._menu_tracking:
            return
        self._flush_menu_update()
        self._menu_tracking = True
        try:
            return callback()
        finally:
            self._menu_tracking = False
            self._flush_menu_update()


def icon_class():
    # Use pywin32's native API bindings for windows and menus. In particular,
    # its window procedures preserve pointer-sized LRESULT values on x64.
    import win32api
    import win32con
    import win32gui
    import ctypes
    from ctypes import wintypes
    from pystray import Menu
    from pystray._win32 import Icon
    from pystray._util import win32
    end_menu = ctypes.WinDLL('user32', use_last_error=True).EndMenu
    end_menu.argtypes = []
    end_menu.restype = wintypes.BOOL

    class TrayIcon(MenuUpdateGuard, Icon):
        _MENU_UPDATE = win32.WM_USER + 100
        menu_renderer = 'winapi_native'

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._menu_paints = 0
            self._popup_paints = 0
            self._menu_opacity_updates = 0
            self._menu_handle = None
            self._message_handlers[self._MENU_UPDATE] = self._flush_menu_update

        def _register_class(self):
            def dispatch(hwnd, message, wparam, lparam):
                if hwnd == getattr(self, '_hwnd', None):
                    handler = getattr(self, '_message_handlers', {}).get(message)
                    if handler is not None:
                        try:
                            return int(handler(wparam, lparam) or 0)
                        except Exception as exc:
                            self._last_menu_error = repr(exc)
                            self._log.exception('Tray message handler failed')
                return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
            cls = win32gui.WNDCLASS()
            cls.hInstance = win32api.GetModuleHandle(None)
            cls.lpszClassName = '%s%dSystemTrayIcon' % (self.name, id(self))
            cls.lpfnWndProc = dispatch
            cls.hbrBackground = win32con.COLOR_WINDOW + 1
            self._native_wndproc = dispatch
            return win32gui.RegisterClass(cls)

        def _unregister_class(self, atom):
            win32gui.UnregisterClass(atom, win32api.GetModuleHandle(None))

        def _create_window(self, atom):
            return win32gui.CreateWindowEx(0, atom, '', win32con.WS_POPUP,
                                           0, 0, 0, 0, 0, 0,
                                           win32api.GetModuleHandle(None), None)

        def _create_menu(self, descriptors, callbacks):
            if not descriptors:
                return None
            menu = win32gui.CreatePopupMenu()
            try:
                for position, descriptor in enumerate(descriptors):
                    callbacks.append(self._handler(descriptor))
                    command = len(callbacks)
                    if descriptor is Menu.SEPARATOR:
                        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')
                        continue
                    flags = win32con.MF_STRING
                    if not descriptor.enabled:
                        flags |= win32con.MF_GRAYED | win32con.MF_DISABLED
                    if descriptor.checked:
                        flags |= win32con.MF_CHECKED
                    if descriptor.submenu:
                        flags |= win32con.MF_POPUP
                        command = self._create_menu(descriptor.submenu, callbacks)
                    try:
                        win32gui.AppendMenu(menu, flags, command, descriptor.text)
                    except Exception:
                        if descriptor.submenu:
                            win32gui.DestroyMenu(command)
                        raise
                    if descriptor.radio:
                        from win32gui_struct import PackMENUITEMINFO
                        info, _refs = PackMENUITEMINFO(fType=win32.MFT_RADIOCHECK)
                        win32gui.SetMenuItemInfo(menu, position, True, info)
                    if descriptor.default:
                        win32gui.SetMenuDefaultItem(menu, position, True)
                return menu
            except Exception:
                win32gui.DestroyMenu(menu)
                raise

        def _post_menu_update(self):
            if self._hwnd:
                win32gui.PostMessage(self._hwnd, self._MENU_UPDATE, 0, 0)

        def _on_notify(self, wparam, lparam):
            if lparam == win32.WM_RBUTTONUP:
                return self._track_popup(self._show_popup)
            return super()._on_notify(wparam, lparam)

        def _show_popup(self):
            if not self._menu_handle:
                return
            owner = self._hwnd
            self._last_menu_error = ''
            try:
                from ._tray_popup import OpaquePopupMenus
            except ImportError:  # Standalone tray helper.
                from _tray_popup import OpaquePopupMenus
            def failed(exc):
                self._last_menu_error = repr(exc)
                self._log.error('Native popup opacity failed: %s', exc)
            def applied():
                self._menu_opacity_updates += 1
            win32gui.SetForegroundWindow(owner)
            x, y = win32gui.GetCursorPos()
            handle, callbacks = self._menu_handle
            try:
                with OpaquePopupMenus(on_error=failed, on_applied=applied):
                    selected = win32gui.TrackPopupMenu(
                        handle, win32con.TPM_RIGHTALIGN | win32con.TPM_BOTTOMALIGN
                        | win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON | 0x4000,
                        x, y, 0, owner, None)
                if 0 < selected <= len(callbacks):
                    callbacks[selected - 1](self)
            finally:
                win32gui.PostMessage(owner, win32con.WM_NULL, 0, 0)

        def _on_stop(self, wparam, lparam):
            if self._menu_tracking:
                end_menu()
            return super()._on_stop(wparam, lparam)

    return TrayIcon
