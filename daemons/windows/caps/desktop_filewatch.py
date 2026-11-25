"""capability: cap:desktop.filewatch  (Windows — watch folders for changes).

Endpoint-bound: watches directories with the OS-level ReadDirectoryChangesW API
(zero-CPU when idle). Ported from ai-pylon apps/file_watch — the env-bound
watcher CORE, reshaped for ai-pylon2.

ai-pylon2 has no daemon→framework EVENT PUSH yet (the agent WebSocket only
carries heartbeat + result), so this cap is a POLL-DRAIN event source instead of
a push: the daemon runs the watchers and buffers change events; a caller drains
them with ``poll``. ReadDirectoryChangesW stays zero-CPU idle, and events
accumulate (bounded ring) until drained — no protocol change needed. (The old
app's webhook/render/test/notify ACTIONS + the event_bus 'files'/'disk' channels
are a follow-up for when an event channel exists; a poller can do the same by
reacting to drained events.)

Methods:
  add(folder, pattern?, recursive?)  -> {ok, folder}         start watching
  remove(folder)                     -> {ok}                 stop watching
  list()                             -> {ok, watches:[...]}
  poll(after?, limit?)               -> {ok, events:[{seq,ts,action,path,...}], last_seq}
    drain buffered change events with seq > after (0 = all buffered).

Degrades to {ok:False} off-Windows.
"""
from __future__ import annotations

import os
import threading
import time

try:
    import ctypes
    _WIN = hasattr(ctypes, "windll")
except Exception:  # noqa: BLE001
    _WIN = False

_BUF_MAX = 2000   # bounded ring of recent change events


class _FileWatch:
    def __init__(self):
        self._watchers = {}   # folder -> DirectoryWatcher
        self._meta = {}       # folder -> {pattern, recursive}
        self._events = []     # ring of {seq, ts, action, path, filename, folder}
        self._seq = 0
        self._lock = threading.Lock()

    def _on_change(self, folder):
        def cb(filepath, action):
            with self._lock:
                self._seq += 1
                self._events.append({
                    "seq": self._seq, "ts": time.time(), "action": action,
                    "path": filepath.replace("\\", "/"),
                    "filename": os.path.basename(filepath),
                    "folder": folder.replace("\\", "/"),
                })
                if len(self._events) > _BUF_MAX:
                    del self._events[:-_BUF_MAX]
        return cb

    def add(self, folder, pattern=None, recursive=True):
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):
            return {"ok": False, "error": "folder does not exist: %s" % folder,
                    "error_category": "application"}
        try:
            from caps._dir_watcher import DirectoryWatcher
        except ImportError:
            from _dir_watcher import DirectoryWatcher  # caps/ on sys.path
        with self._lock:
            if folder in self._watchers:
                return {"ok": True, "folder": folder, "already": True}
            w = DirectoryWatcher(folder, self._on_change(folder),
                                 recursive=bool(recursive), pattern=pattern or None)
            w.start()
            self._watchers[folder] = w
            self._meta[folder] = {"pattern": pattern or None, "recursive": bool(recursive)}
        return {"ok": True, "folder": folder}

    def remove(self, folder):
        folder = os.path.abspath(folder)
        with self._lock:
            w = self._watchers.pop(folder, None)
            self._meta.pop(folder, None)
        if w is None:
            return {"ok": True, "removed": False}
        try:
            w.stop()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "removed": True, "folder": folder}

    def list(self):
        with self._lock:
            return {"ok": True, "watches": [
                {"folder": f, **self._meta.get(f, {})} for f in self._watchers]}

    def poll(self, after=0, limit=500):
        with self._lock:
            evs = [e for e in self._events if e["seq"] > after][:limit]
            last = self._events[-1]["seq"] if self._events else after
        return {"ok": True, "events": evs, "last_seq": last}


_FW = _FileWatch() if _WIN else None


def _na():
    return {"ok": False, "error": "filewatch is Windows-only on this endpoint",
            "error_category": "unavailable"}


def add(args, ctx):
    if not _WIN:
        return _na()
    folder = (args.get("folder") or "").strip()
    if not folder:
        return {"ok": False, "error": "folder required", "error_category": "application"}
    return _FW.add(folder, args.get("pattern"), args.get("recursive", True))


def remove(args, ctx):
    if not _WIN:
        return _na()
    folder = (args.get("folder") or "").strip()
    if not folder:
        return {"ok": False, "error": "folder required", "error_category": "application"}
    return _FW.remove(folder)


def list_watches(args, ctx):
    if not _WIN:
        return _na()
    return _FW.list()


def poll(args, ctx):
    if not _WIN:
        return _na()
    try:
        after = int(args.get("after", 0) or 0)
    except (TypeError, ValueError):
        after = 0
    try:
        limit = max(1, min(2000, int(args.get("limit", 500) or 500)))
    except (TypeError, ValueError):
        limit = 500
    return _FW.poll(after, limit)


METHODS = {"add": add, "remove": remove, "list": list_watches, "poll": poll}
CAPABILITY = "cap:desktop.filewatch"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
