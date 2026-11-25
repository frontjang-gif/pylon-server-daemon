"""capability: cap:desktop.proc  (Windows — safe process classification + kill).

Endpoint-bound. Grounds three cleanup lessons from the v1-folder-removal session
into a real capability instead of ad-hoc ctypes hunts (CLEANUP #10/#11/#23):

Methods:
  terminate({pid, confirm})        -> {ok, pid, method}
        Kill a process by PID using raw OpenProcess(PROCESS_TERMINATE) +
        TerminateProcess — the path that KILLED a higher-integrity same-user
        process this session where Stop-Process AND `taskkill /F` both returned
        "Access is denied". By PID ONLY (never by fuzzy name — v1's stop.bat
        substring-matched 'ai-pylon' and killed 2 v2 daemons). Self-gated on
        confirm=true; refuses to kill the daemon itself or PID 0/4.
  classify({pids?})                -> {ok, procs:[{pid,name,user,integrity,
                                        cmdline,ppid}]}
        Classify processes safely for cleanup: owner (user), integrity level
        (System/High/Medium/Low — the Medium-vs-High distinction that told v1's
        elevated daemons apart from v2's), command line, parent pid. A single
        batched snapshot (#23) rather than N per-pid ctypes token probes.
  find({name?, user?, min_integrity?, cmdline_contains?})
                                   -> {ok, procs:[...]}  (classify + filter)

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

# Win32 constants
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008

# integrity RIDs
_INTEGRITY = [
    (0x4000, "System"),
    (0x3000, "High"),
    (0x2000, "Medium"),
    (0x1000, "Low"),
    (0x0000, "Untrusted"),
]
_INTEGRITY_RANK = {name: i for i, (_, name) in enumerate(reversed(_INTEGRITY))}


def _na():
    return {"ok": False, "error": "cap:desktop.proc is Windows-only",
            "error_category": "unavailable"}


def status(args, ctx):
    return {"ok": True, "available": _WIN}


def _own_pids():
    """Our own pid + parent, never to be killed by this cap."""
    out = set()
    try:
        out.add(os.getpid())
        out.add(os.getppid())
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- classification
def _toolhelp_snapshot():
    """All processes via CreateToolhelp32Snapshot -> {pid: {name, ppid}}."""
    import ctypes as C
    k32 = C.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(C.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", C.POINTER(C.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", C.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", C.c_char * 260),
        ]

    procs = {}
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return procs
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = C.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, C.byref(entry))
        while ok:
            try:
                name = entry.szExeFile.decode("mbcs", "replace")
            except Exception:  # noqa: BLE001
                name = ""
            procs[int(entry.th32ProcessID)] = {
                "name": name, "ppid": int(entry.th32ParentProcessID)}
            ok = k32.Process32Next(snap, C.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return procs


def _integrity_of(pid):
    """Integrity level name for a pid via its token, or None."""
    import ctypes as C
    k32 = C.windll.kernel32
    advapi = C.windll.advapi32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    advapi.GetSidSubAuthorityCount.restype = C.POINTER(C.c_ubyte)
    advapi.GetSidSubAuthorityCount.argtypes = [C.c_void_p]
    advapi.GetSidSubAuthority.restype = C.POINTER(wintypes.DWORD)
    advapi.GetSidSubAuthority.argtypes = [C.c_void_p, wintypes.DWORD]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi.OpenProcessToken(h, TOKEN_QUERY, C.byref(token)):
            return None
        # TokenIntegrityLevel = 25
        size = wintypes.DWORD(0)
        advapi.GetTokenInformation(token, 25, None, 0, C.byref(size))
        if size.value == 0:
            return None
        buf = (C.c_byte * size.value)()
        if not advapi.GetTokenInformation(token, 25, buf, size, C.byref(size)):
            return None
        # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label { PSID Sid; DWORD Attr } }
        psid = C.cast(buf, C.POINTER(C.c_void_p))[0]
        count = advapi.GetSidSubAuthorityCount(psid)[0]
        rid = advapi.GetSidSubAuthority(psid, count - 1)[0]
        name = "Untrusted"
        for threshold, label in _INTEGRITY:
            if rid >= threshold:
                name = label
                break
        return name
    finally:
        try:
            if token:
                k32.CloseHandle(token)
            k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass


def _owner_of(pid):
    """Domain\\user owning a pid, or None (best-effort)."""
    import ctypes as C
    k32 = C.windll.kernel32
    advapi = C.windll.advapi32
    advapi.LookupAccountSidA.argtypes = [
        C.c_char_p, C.c_void_p, C.c_char_p, C.POINTER(wintypes.DWORD),
        C.c_char_p, C.POINTER(wintypes.DWORD), C.POINTER(wintypes.DWORD)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi.OpenProcessToken(h, TOKEN_QUERY, C.byref(token)):
            return None
        size = wintypes.DWORD(0)
        advapi.GetTokenInformation(token, 1, None, 0, C.byref(size))  # TokenUser=1
        if size.value == 0:
            return None
        buf = (C.c_byte * size.value)()
        if not advapi.GetTokenInformation(token, 1, buf, size, C.byref(size)):
            return None
        psid = C.cast(buf, C.POINTER(C.c_void_p))[0]
        name = (C.c_char * 256)()
        domain = (C.c_char * 256)()
        cbn = wintypes.DWORD(256)
        cbd = wintypes.DWORD(256)
        use = wintypes.DWORD(0)
        if advapi.LookupAccountSidA(None, psid, name, C.byref(cbn),
                                    domain, C.byref(cbd), C.byref(use)):
            d = domain.value.decode("mbcs", "replace")
            n = name.value.decode("mbcs", "replace")
            return ("%s\\%s" % (d, n)) if d else n
        return None
    finally:
        try:
            if token:
                k32.CloseHandle(token)
            k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass


def _cmdline_of(pid):
    """Best-effort command line via WMI (wmic is deprecated but still present);
    returns '' if unavailable. Kept cheap — only called when asked."""
    try:
        import subprocess
        r = subprocess.run(
            ["wmic", "process", "where", "ProcessId=%d" % pid, "get",
             "CommandLine", "/value"],
            capture_output=True, text=True, timeout=10)
        for ln in (r.stdout or "").splitlines():
            if ln.startswith("CommandLine="):
                return ln[len("CommandLine="):].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _classify(pids=None, want_cmdline=False):
    snap = _toolhelp_snapshot()
    targets = pids if pids else list(snap.keys())
    out = []
    for pid in targets:
        info = snap.get(pid, {})
        rec = {
            "pid": pid,
            "name": info.get("name", ""),
            "ppid": info.get("ppid"),
            "integrity": _integrity_of(pid),
            "user": _owner_of(pid),
            "cmdline": _cmdline_of(pid) if want_cmdline else None,
        }
        out.append(rec)
    return out


def classify(args, ctx):
    if not _WIN:
        return _na()
    a = args or {}
    pids = a.get("pids")
    try:
        pids = [int(p) for p in pids] if pids else None
    except (TypeError, ValueError):
        return {"ok": False, "error": "pids must be ints", "error_category": "application"}
    procs = _classify(pids, want_cmdline=bool(a.get("cmdline")))
    return {"ok": True, "procs": procs, "count": len(procs)}


def find(args, ctx):
    if not _WIN:
        return _na()
    a = args or {}
    procs = _classify(None, want_cmdline=bool(a.get("cmdline") or a.get("cmdline_contains")))
    name = (a.get("name") or "").lower()
    user = (a.get("user") or "").lower()
    cc = (a.get("cmdline_contains") or "").lower()
    min_int = a.get("min_integrity")
    min_rank = _INTEGRITY_RANK.get(min_int) if min_int else None

    def _keep(p):
        if name and name not in (p.get("name") or "").lower():
            return False
        if user and user not in (p.get("user") or "").lower():
            return False
        if cc and cc not in (p.get("cmdline") or "").lower():
            return False
        if min_rank is not None:
            r = _INTEGRITY_RANK.get(p.get("integrity"))
            if r is None or r < min_rank:
                return False
        return True

    kept = [p for p in procs if _keep(p)]
    return {"ok": True, "procs": kept, "count": len(kept)}


def terminate(args, ctx):
    """Raw OpenProcess(PROCESS_TERMINATE)+TerminateProcess by PID. Self-gated on
    confirm=true; refuses PID 0/4 (System/Idle) and this daemon's own pid/ppid."""
    if not _WIN:
        return _na()
    a = args or {}
    try:
        pid = int(a.get("pid"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "pid (int) required", "error_category": "application"}
    if not a.get("confirm"):
        # dry-run preview (the safety contract for a confirm op)
        info = _classify([pid], want_cmdline=True)
        return {"ok": True, "dry_run": True, "pid": pid,
                "would_terminate": info[0] if info else {"pid": pid},
                "hint": "re-call with confirm=true to actually terminate"}
    if pid in (0, 4):
        return {"ok": False, "error": "refusing to terminate PID %d (System/Idle)" % pid,
                "error_category": "application"}
    if pid in _own_pids():
        return {"ok": False, "error": "refusing to terminate the daemon itself (pid %d)" % pid,
                "error_category": "application"}
    import ctypes as C
    k32 = C.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        err = C.get_last_error() if hasattr(C, "get_last_error") else 0
        return {"ok": False, "error": "OpenProcess(PROCESS_TERMINATE) failed (pid %d, err %s)"
                % (pid, err), "error_category": "application"}
    try:
        ok = k32.TerminateProcess(h, 1)
    finally:
        k32.CloseHandle(h)
    if not ok:
        return {"ok": False, "error": "TerminateProcess failed (pid %d)" % pid,
                "error_category": "application"}
    return {"ok": True, "pid": pid, "method": "OpenProcess+TerminateProcess"}


METHODS = {"status": status, "classify": classify, "find": find, "terminate": terminate}
CAPABILITY = "cap:desktop.proc"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
