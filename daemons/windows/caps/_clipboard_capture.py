"""Windows clipboard format reader."""
from dataclasses import dataclass
import ctypes
import io
import os
import time

try:
    from ._clipboard_source_context import SourceContextResolver
except ImportError:  # loaded as a top-level module (caps/ on sys.path)
    from _clipboard_source_context import SourceContextResolver


@dataclass(frozen=True)
class ClipboardSnapshot:
    kind: str
    content: object
    display: str
    metadata: dict
    image_path: str | None = None


class WindowsClipboardCapture:
    """Read a clipboard snapshot without coupling capture to persistence."""

    CF_UNICODETEXT = 13
    CF_HDROP = 15
    CF_DIB = 8

    def __init__(self, images_dir, context_resolver=None, clock=time.time,
                 capture_context=True):
        self.images_dir = images_dir
        self.context = context_resolver or SourceContextResolver()
        self.clock = clock
        self.capture_context = capture_context
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.shell32 = ctypes.windll.shell32
        self._configure_apis()
        self.cf_html = self.user32.RegisterClipboardFormatW('HTML Format')

    def sequence_number(self):
        return self.user32.GetClipboardSequenceNumber()

    def read(self):
        owner = self.context.clipboard_owner() if self.capture_context else None
        for _attempt in range(3):
            if self.user32.OpenClipboard(0):
                try:
                    files = self._read_files()
                    html = self._read_global_bytes(self.cf_html)
                    text = self._read_text()
                    image_path = None if files or html else self._read_image()
                    return self._snapshot(owner, files, html, text, image_path)
                finally:
                    self.user32.CloseClipboard()
            time.sleep(0.05)
        return None

    def _snapshot(self, owner, files, html_bytes, text, image_path):
        html = html_bytes.decode('utf-8', errors='replace') if html_bytes else None
        metadata = (
            self.context.resolve(owner, html=html, files=files, text=text)
            if self.capture_context else {}
        )
        if files:
            return ClipboardSnapshot(
                'files', files, '\n'.join(files), metadata)
        if html:
            return ClipboardSnapshot(
                'html', html, self._html_fragment(html), metadata)
        if image_path:
            return ClipboardSnapshot(
                'image', os.path.basename(image_path),
                os.path.basename(image_path), metadata, image_path)
        if text is not None:
            return ClipboardSnapshot('text', text, text, metadata)
        return None

    def _read_files(self):
        if not self.user32.IsClipboardFormatAvailable(self.CF_HDROP):
            return None
        handle = self.user32.GetClipboardData(self.CF_HDROP)
        if not handle:
            return None
        count = self.shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        files = []
        for index in range(count):
            length = self.shell32.DragQueryFileW(handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            self.shell32.DragQueryFileW(handle, index, buffer, len(buffer))
            files.append(buffer.value)
        return files or None

    def _read_text(self):
        if not self.user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
            return None
        handle = self.user32.GetClipboardData(self.CF_UNICODETEXT)
        pointer = self.kernel32.GlobalLock(handle) if handle else None
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            self.kernel32.GlobalUnlock(handle)

    def _read_global_bytes(self, clipboard_format):
        if not self.user32.IsClipboardFormatAvailable(clipboard_format):
            return None
        handle = self.user32.GetClipboardData(clipboard_format)
        pointer = self.kernel32.GlobalLock(handle) if handle else None
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, self.kernel32.GlobalSize(handle))
        finally:
            self.kernel32.GlobalUnlock(handle)

    def _read_image(self):
        data = self._read_global_bytes(self.CF_DIB)
        if not data:
            return None
        filename = f'{int(self.clock() * 1000)}.png'
        path = os.path.join(self.images_dir, filename)
        bitmap = (
            b'BM' + (len(data) + 14).to_bytes(4, 'little')
            + b'\x00\x00\x00\x00\x36\x00\x00\x00' + data
        )
        try:
            from PIL import Image
            Image.open(io.BytesIO(bitmap)).save(path, 'PNG')
        except Exception:
            path = os.path.splitext(path)[0] + '.bmp'
            with open(path, 'wb') as handle:
                handle.write(bitmap)
        return path

    def _configure_apis(self):
        self.user32.GetClipboardData.restype = ctypes.c_void_p
        self.user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        self.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        self.kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        self.kernel32.GlobalSize.restype = ctypes.c_size_t
        self.shell32.DragQueryFileW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
        self.shell32.DragQueryFileW.restype = ctypes.c_uint

    @staticmethod
    def _html_fragment(html):
        start_marker = '<!--StartFragment-->'
        end_marker = '<!--EndFragment-->'
        start = html.find(start_marker)
        end = html.find(end_marker)
        if start >= 0 and end >= 0:
            return html[start + len(start_marker):end]
        return html
