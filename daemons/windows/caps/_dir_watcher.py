"""OS-level directory watcher using Windows ReadDirectoryChangesW.

Ported from ai-pylon pylon/framework/base/inc/file_watcher.py — pure ctypes,
zero-CPU when idle, instant notification. The only change from the original is
dropping the pylon ``process_tasks`` dependency for a plain daemon thread.

    def on_change(filepath, action): ...
    w = DirectoryWatcher('C:/dir', on_change, recursive=True); w.start(); w.stop()
"""
import ctypes
import ctypes.wintypes
import logging
import os
import threading
import time

FILE_NOTIFY_CHANGE_FILE_NAME = 0x01
FILE_NOTIFY_CHANGE_DIR_NAME = 0x02
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
FILE_NOTIFY_CHANGE_SIZE = 0x08
FILE_LIST_DIRECTORY = 0x01
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

FILE_ACTION_ADDED = 1
FILE_ACTION_REMOVED = 2
FILE_ACTION_MODIFIED = 3
FILE_ACTION_RENAMED_OLD = 4
FILE_ACTION_RENAMED_NEW = 5

ACTION_NAMES = {
    FILE_ACTION_ADDED: 'created',
    FILE_ACTION_REMOVED: 'deleted',
    FILE_ACTION_MODIFIED: 'modified',
    FILE_ACTION_RENAMED_OLD: 'renamed_from',
    FILE_ACTION_RENAMED_NEW: 'renamed_to',
}


class DirectoryWatcher:
    def __init__(self, path, callback, recursive=True, pattern=None):
        self.path = os.path.abspath(path)
        self.callback = callback
        self.recursive = recursive
        self.pattern = pattern
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._watch, name='watcher-%s' % os.path.basename(self.path),
            daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _watch(self):
        while self._running:
            try:
                self._watch_once()
            except Exception:
                logging.getLogger(__name__).debug("suppressed exception", exc_info=True)
            if self._running:
                time.sleep(2)

    def _watch_once(self):
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            self.path, FILE_LIST_DIRECTORY, 0x01 | 0x02 | 0x04, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if handle == ctypes.wintypes.HANDLE(-1).value:
            return
        buf = ctypes.create_string_buffer(4096)
        bytes_returned = ctypes.wintypes.DWORD()
        notify_filter = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
                         | FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_SIZE)
        try:
            while self._running:
                result = kernel32.ReadDirectoryChangesW(
                    handle, buf, len(buf), self.recursive, notify_filter,
                    ctypes.byref(bytes_returned), None, None)
                if not result:
                    break
                offset = 0
                while True:
                    fni = buf.raw[offset:]
                    next_offset = int.from_bytes(fni[0:4], 'little')
                    action = int.from_bytes(fni[4:8], 'little')
                    name_len = int.from_bytes(fni[8:12], 'little')
                    filename = fni[12:12 + name_len].decode('utf-16-le')
                    filepath = os.path.join(self.path, filename)
                    if not self.pattern or filepath.endswith(self.pattern):
                        self._notify(filepath, action)
                    if next_offset == 0:
                        break
                    offset += next_offset
        finally:
            kernel32.CloseHandle(handle)

    def _notify(self, filepath, action):
        try:
            self.callback(filepath, ACTION_NAMES.get(action, 'unknown'))
        except Exception:
            logging.getLogger(__name__).debug("suppressed exception", exc_info=True)
