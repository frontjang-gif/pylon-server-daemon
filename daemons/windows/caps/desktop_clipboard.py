"""capability: cap:desktop.clipboard  (Windows — read the system clipboard).

Endpoint-bound: reads the current clipboard contents (text / files / HTML /
image) via the Win32 clipboard API, plus the source-app context (which window
copied it). Pure ctypes — no pywin32. Ported from ai-pylon apps/system/clipboard
(the capture CORE in capture.py + source_context.py, here as private
`_clipboard_capture` / `_clipboard_source_context` helpers).

This cap is the CAPTURE primitive only. The old clipboard app's history DB,
pattern TRIGGERS, daily archive, and web UI are NOT part of the capability — an
app that wants clipboard history can poll `sequence` + `read` and keep its own
store (or subscribe to a future clipboard event channel). See the migration
tracker for that follow-up.

Methods:
  read()       -> {ok, kind, text, metadata, image_path?}  (current clipboard)
  sequence()   -> {ok, sequence}  (GetClipboardSequenceNumber — poll for change)

Degrades to {ok:False} off-Windows.
"""
from __future__ import annotations

import os

try:
    import ctypes
    _WIN = hasattr(ctypes, "windll")
except Exception:  # noqa: BLE001
    _WIN = False

_CAP = None


def _images_dir():
    root = os.environ.get("PYLON2_DATA_DIR")
    if not root:
        # this file lives at daemons/windows/caps/ — the repo root is four
        # levels up (caps -> windows -> daemons -> repo root). Matches the
        # resolution in _edge_profile.py so runtime state lands under the
        # repo-root data/ dir, never nested inside the daemons/ code folder.
        here = os.path.abspath(__file__)
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
        root = os.path.join(repo_root, "data")
    d = os.path.join(root, "daemons", "windows", "clipboard-images")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _capture():
    global _CAP
    if _CAP is None:
        try:
            from caps._clipboard_capture import WindowsClipboardCapture
        except ImportError:
            from _clipboard_capture import WindowsClipboardCapture  # caps/ on sys.path
        _CAP = WindowsClipboardCapture(_images_dir())
    return _CAP


def _na():
    return {"ok": False, "error": "clipboard is Windows-only on this endpoint",
            "error_category": "unavailable"}


def read(args, ctx):
    if not _WIN:
        return _na()
    try:
        snap = _capture().read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "clipboard read failed: %s" % exc,
                "error_category": "application"}
    if snap is None:
        return {"ok": True, "kind": "empty", "text": "", "metadata": {}}
    return {"ok": True, "kind": snap.kind, "text": snap.display,
            "metadata": snap.metadata or {}, "image_path": snap.image_path}


def sequence(args, ctx):
    if not _WIN:
        return _na()
    try:
        return {"ok": True, "sequence": int(_capture().sequence_number())}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


METHODS = {"read": read, "sequence": sequence}
CAPABILITY = "cap:desktop.clipboard"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
