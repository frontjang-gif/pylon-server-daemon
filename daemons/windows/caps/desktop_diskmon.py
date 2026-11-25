"""capability: cap:desktop.diskmon  (Windows — disk usage + process I/O + file owner).

Endpoint-bound: OS-level disk inspection via ctypes (no third-party deps, no
elevation for these methods). Ported from ai-pylon apps/file/disk — the
elevation-FREE subset: per-volume disk usage, per-process write-byte counters
(GetProcessIoCounters over a Toolhelp snapshot), and which process holds a file
open (Restart Manager RmGetList). The v1 app's real-time ETW file-write tracer
needs admin + a kernel session; it's reported as needs_elevation here rather than
shipped half-working.

Methods:
  status()                       -> {ok, available, elevated}
  drives()                       -> {ok, drives:[{drive,total,used,free,percent}]}
  top_io({limit?})               -> {ok, processes:[{pid,name,write_bytes}], count}
  file_owner({path})             -> {ok, owner:{name,pid,exe}|null}

Degrades to {ok:False, error_category:unavailable} off-Windows.
"""
from __future__ import annotations

import os

try:
    import ctypes
    from ctypes import wintypes
    _WIN = hasattr(ctypes, "windll")
except Exception:  # noqa: BLE001
    _WIN = False

if _WIN:
    _kernel32 = ctypes.windll.kernel32
    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


def _na():
    return {"ok": False, "error": "diskmon is Windows-only on this endpoint",
            "error_category": "unavailable"}


def _is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _exe_path(pid):
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        _kernel32.CloseHandle(h)
    return ""


def _snapshot_proc_io():
    """{pid: (name, write_bytes)} for every process we can open."""
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return {}
    pe = _PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    procs = {}
    try:
        if _kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                pid = pe.th32ProcessID
                name = pe.szExeFile
                h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if h:
                    io = _IO_COUNTERS()
                    if _kernel32.GetProcessIoCounters(h, ctypes.byref(io)):
                        procs[pid] = (name, int(io.WriteTransferCount))
                    _kernel32.CloseHandle(h)
                if not _kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        _kernel32.CloseHandle(snap)
    return procs


def _file_owner(filepath):
    """Which process holds *filepath* open, via Restart Manager. (name,pid,exe) or None."""
    try:
        rm = ctypes.windll.rstrtmgr
    except OSError:
        return None
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(256)
    if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return None
    try:
        path_buf = ctypes.c_wchar_p(filepath)
        if rm.RmRegisterResources(session, 1, ctypes.byref(path_buf), 0, None, 0, None) != 0:
            return None

        class _RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD),
                        ("ProcessStartTime", ctypes.c_ulonglong)]

        class _RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [
                ("Process", _RM_UNIQUE_PROCESS),
                ("strAppName", ctypes.c_wchar * 256),
                ("strServiceShortName", ctypes.c_wchar * 64),
                ("ApplicationType", wintypes.DWORD), ("AppStatus", wintypes.DWORD),
                ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL),
            ]

        n_needed = wintypes.UINT()
        n_procs = wintypes.UINT(16)
        procs = (_RM_PROCESS_INFO * 16)()
        reasons = wintypes.DWORD()
        res = rm.RmGetList(session, ctypes.byref(n_needed), ctypes.byref(n_procs),
                           procs, ctypes.byref(reasons))
        if res == 0 and n_procs.value > 0:
            info = procs[0]
            pid = info.Process.dwProcessId
            return (info.strAppName, pid, _exe_path(pid))
    finally:
        rm.RmEndSession(session)
    return None


# ------------------------------------------------------------------ methods --
def status(args, ctx):
    return {"ok": True, "available": _WIN, "elevated": _is_elevated() if _WIN else False}


def drives(args, ctx):
    if not _WIN:
        return _na()
    out = []
    bitmask = _kernel32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask & (1 << i)):
            continue
        d = "%s:\\" % chr(ord("A") + i)
        try:
            usage = __import__("shutil").disk_usage(d)
            out.append({"drive": d, "total": usage.total, "used": usage.used,
                        "free": usage.free,
                        "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0})
        except OSError:
            continue  # empty removable/CD drive
    return {"ok": True, "drives": out, "count": len(out)}


def top_io(args, ctx):
    if not _WIN:
        return _na()
    try:
        limit = max(1, min(200, int((args or {}).get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    snap = _snapshot_proc_io()
    rows = sorted(({"pid": pid, "name": name, "write_bytes": wb}
                   for pid, (name, wb) in snap.items()),
                  key=lambda r: r["write_bytes"], reverse=True)[:limit]
    return {"ok": True, "processes": rows, "count": len(rows)}


def file_owner(args, ctx):
    if not _WIN:
        return _na()
    path = ((args or {}).get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path required", "error_category": "application"}
    if not os.path.exists(path):
        return {"ok": False, "error": "not found: %s" % path, "error_category": "application"}
    owner = _file_owner(path)
    if owner is None:
        return {"ok": True, "owner": None}
    name, pid, exe = owner
    return {"ok": True, "owner": {"name": name, "pid": pid, "exe": exe}}


METHODS = {"status": status, "drives": drives, "top_io": top_io, "file_owner": file_owner}
CAPABILITY = "cap:desktop.diskmon"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
