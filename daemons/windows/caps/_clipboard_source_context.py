"""Best-effort source metadata for Windows clipboard changes."""
import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path
from urllib.parse import unquote, urlparse


_SOURCE_URL = re.compile(r'^SourceURL:(.+)$', re.IGNORECASE | re.MULTILINE)


def source_url_from_html(html):
    """Return the CF_HTML SourceURL header when the producer supplied one."""
    match = _SOURCE_URL.search(html or '')
    return match.group(1).strip() if match else None


def file_path_from_url(url):
    parsed = urlparse(url or '')
    if parsed.scheme.lower() != 'file':
        return None
    path = unquote(parsed.path)
    if parsed.netloc:
        return f'//{parsed.netloc}{path}'.replace('/', os.sep)
    if re.match(r'^/[A-Za-z]:', path):
        path = path[1:]
    return path.replace('/', os.sep)


def url_from_text(text):
    """Recognize a complete HTTP(S) URL without guessing from prose."""
    candidate = (text or '').strip()
    if any(character.isspace() for character in candidate):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme.lower() in {'http', 'https'} and parsed.netloc:
        return candidate
    return None


def notepad_path_from_state(data):
    """Decode the absolute path from a modern Notepad tab-state record."""
    if not data.startswith(b'NP\x00\x01B'):
        return None
    payload = data[5:]
    terminator = payload.find(b'\x00\x00')
    if terminator >= 0:
        # The first byte belongs to the final UTF-16 character's high byte;
        # retain it and omit the following field delimiter.
        payload = payload[:terminator + 1]
    path = payload.decode('utf-16-le', errors='ignore')
    return path if os.path.isabs(path) else None


def notepad_document_path(window_title, local_state=None):
    """Match Notepad's active filename to its read-only tab-state path."""
    if not window_title:
        return None
    state_root = Path(local_state or (
        Path(os.environ.get('LOCALAPPDATA', ''))
        / 'Packages' / 'Microsoft.WindowsNotepad_8wekyb3d8bbwe'
        / 'LocalState' / 'TabState'
    ))
    matches = []
    try:
        state_files = state_root.glob('*.bin')
        for state_file in state_files:
            try:
                path = notepad_path_from_state(state_file.read_bytes())
                if path and os.path.basename(path).casefold() in window_title.casefold():
                    matches.append((state_file.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return None
    return max(matches)[1] if matches else None


class SourceContextResolver:
    """Resolve clipboard owner, page URL, file paths, and Office context."""

    def __init__(self, window_info=None, office_context=None):
        self._window_info = window_info or self._windows_window_info
        self._office_context = office_context or self._windows_office_context

    def resolve(self, owner_window=None, html=None, files=None, text=None):
        metadata = {}
        provenance = {}
        window = self._window_info(owner_window) if owner_window else {}
        metadata.update(window)
        if window:
            provenance['application'] = 'clipboard_owner'

        url = source_url_from_html(html) or url_from_text(text)
        if url:
            metadata['source_url'] = url
            provenance['source_url'] = (
                'cf_html' if source_url_from_html(html) else 'clipboard_text')
            file_path = file_path_from_url(url)
            if file_path:
                metadata['source_path'] = file_path
                provenance['source_path'] = 'cf_html'

        if files:
            metadata['source_paths'] = list(files)
            metadata['source_path'] = files[0]
            provenance['source_path'] = 'cf_hdrop'

        app_name = metadata.get('source_app', '').lower()
        if app_name in {'winword.exe', 'excel.exe', 'powerpnt.exe'}:
            office = self._office_context(app_name)
            if office:
                if office.get('source_path'):
                    provenance['source_path'] = 'office_com'
                metadata.update(office)
        elif app_name == 'notepad.exe':
            document_path = notepad_document_path(
                metadata.get('source_window', ''))
            if document_path:
                metadata['source_path'] = document_path
                provenance['source_path'] = 'notepad_tab_state'

        if provenance:
            metadata['provenance'] = provenance
        return metadata

    @staticmethod
    def clipboard_owner():
        if os.name != 'nt':
            return None
        user32 = ctypes.windll.user32
        user32.GetClipboardOwner.restype = ctypes.c_void_p
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        owner = user32.GetClipboardOwner()
        return owner or user32.GetForegroundWindow() or None

    @staticmethod
    def _windows_window_info(window):
        if os.name != 'nt' or not window:
            return {}
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = ctypes.c_void_p
        root_window = user32.GetAncestor(window, 2) or window  # GA_ROOT
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(root_window, ctypes.byref(process_id))
        title_length = user32.GetWindowTextLengthW(root_window)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(root_window, title_buffer, len(title_buffer))
        if not title_buffer.value:
            title_buffer = SourceContextResolver._process_window_title(
                process_id.value)
        executable = SourceContextResolver._process_path(process_id.value)
        result = {'source_pid': process_id.value}
        if executable:
            result['source_app'] = os.path.basename(executable)
            result['source_executable'] = executable
        if title_buffer.value:
            result['source_window'] = title_buffer.value
        return result

    @staticmethod
    def _process_window_title(process_id):
        """Return a visible top-level title for a clipboard-owner process."""
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p

        # Copying normally leaves the source window in the foreground. Prefer
        # that exact window when several Notepad tabs/windows share a process.
        foreground = user32.GetForegroundWindow()
        if foreground:
            foreground_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(
                foreground, ctypes.byref(foreground_pid))
            if foreground_pid.value == process_id:
                length = user32.GetWindowTextLengthW(foreground)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(foreground, buffer, len(buffer))
                    if buffer.value:
                        return buffer

        titles = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)

        def collect(window, _parameter):
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(window_pid))
            if window_pid.value == process_id and user32.IsWindowVisible(window):
                length = user32.GetWindowTextLengthW(window)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(window, buffer, len(buffer))
                    if buffer.value:
                        titles.append(buffer.value)
            return True

        user32.EnumWindows(callback_type(collect), 0)
        value = max(titles, key=len) if titles else ''
        return ctypes.create_unicode_buffer(value)

    @staticmethod
    def _process_path(process_id):
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
        finally:
            kernel32.CloseHandle(handle)
        return None

    @staticmethod
    def _windows_office_context(app_name):
        """Read the active Office document when pywin32 and COM allow it."""
        try:
            import win32com.client
            prog_id = {
                'winword.exe': 'Word.Application',
                'excel.exe': 'Excel.Application',
                'powerpnt.exe': 'PowerPoint.Application',
            }[app_name]
            app = win32com.client.GetActiveObject(prog_id)
            if app_name == 'winword.exe':
                document = app.ActiveDocument
                selection = app.Selection
                return {
                    'source_path': document.FullName,
                    'selection': {
                        'start': int(selection.Start),
                        'end': int(selection.End),
                    },
                }
            if app_name == 'excel.exe':
                return {
                    'source_path': app.ActiveWorkbook.FullName,
                    'selection': {'address': app.Selection.Address},
                }
            return {'source_path': app.ActivePresentation.FullName}
        except Exception:
            return {}
