"""capability: cap:system.terminal  (Windows endpoint — real shells live here).

Endpoint-bound: a terminal is the user's actual machine, so the shells run on
the DAEMON (not in a portable app). Ported from v1's endpoint app, keeping the
proven model — one persistent child per session, a background reader thread
draining stdout into a lock-guarded ring buffer, caller-supplied session ids,
and an idle reaper — but adding the piece v1 lacked: a proper ONE-SHOT exec that
runs a command to completion and returns a REAL exit code with stdout/stderr
split (v1's only exit-code path was SSH).

Backends (local only for now; ssh/wsl/docker are the documented next step and
slot behind the same tiny duck-typed contract create/write/read/resize/kill):
  * local — a true PTY via pywinpty/ConPTY (interactive shell sessions)
  * one-shot `run` — subprocess.run (not a session): exit code + stdout/stderr

Methods:
  run(cmd, cwd?, shell?, timeout=60)         -> {ok, stdout, stderr, exit_code, duration_ms}
  create(id='default', shell?, cwd?, cols?, rows?) -> {ok, id}
  write(id, data)                            -> {ok}
  read(id)                                   -> {ok, output, alive}
  exec(id, cmd, timeout=15)                  -> {ok, output, alive}  (quiescence; interactive)
  resize(id, cols, rows)                     -> {ok}
  kill(id)                                   -> {ok}
  list()                                     -> {ok, sessions:[{id, alive, shell, uptime}]}

Degrades cleanly: interactive sessions need `pywinpty`; if it's missing, `run`
(one-shot) still works and create() returns a clear 'unavailable'.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

_IDLE_TIMEOUT = 30 * 60          # reap sessions idle longer than this
_SESSIONS = {}                    # id -> session dict
_LOCK = threading.Lock()
_reaper_started = False


def _winpty():
    try:
        from winpty import PtyProcess  # pywinpty
        return PtyProcess
    except Exception:  # noqa: BLE001
        return None


def _start_reaper():
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def _loop():
        while True:
            time.sleep(60)
            now = time.time()
            for sid, s in list(_SESSIONS.items()):
                if now - s.get("last", now) > _IDLE_TIMEOUT:
                    _kill(sid)
    threading.Thread(target=_loop, name="term-reaper", daemon=True).start()


def _touch(s):
    s["last"] = time.time()


# --- one-shot exec (NEW vs v1: real exit code, split stdout/stderr) ---------

def run(args, ctx):
    """Run a command to completion and return a real exit code. Not a session.
    Uses the system shell so pipes/builtins work, but hides the window and bounds
    the runtime. This is the clean 'run → {exit_code, stdout, stderr}' path."""
    cmd = args.get("cmd") or args.get("command") or ""
    if not cmd:
        return {"ok": False, "error": "cmd required", "error_category": "application"}
    cwd = args.get("cwd") or None
    timeout = float(args.get("timeout") or 60)
    shell = args.get("shell") or os.environ.get("COMSPEC", "cmd.exe")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    t0 = time.monotonic()
    try:
        # cmd.exe /c  (or powershell -Command) runs the line and exits.
        if "powershell" in shell.lower() or "pwsh" in shell.lower():
            argv = [shell, "-NoProfile", "-NonInteractive", "-Command", cmd]
        else:
            argv = [shell, "/c", cmd]
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=max(0.1, timeout), startupinfo=si,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        return {"ok": True, "stdout": done.stdout or "", "stderr": done.stderr or "",
                "exit_code": done.returncode,
                "duration_ms": int((time.monotonic() - t0) * 1000)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "error_category": "timeout",
                "duration_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


# --- interactive PTY sessions (v1 model) ------------------------------------

def create(args, ctx):
    PtyProcess = _winpty()
    if PtyProcess is None:
        return {"ok": False, "error": "pywinpty not installed (interactive sessions "
                "unavailable; one-shot `run` still works)", "error_category": "unavailable"}
    _start_reaper()
    sid = args.get("id") or "default"
    shell = args.get("shell") or os.environ.get("COMSPEC", "cmd.exe")
    cwd = args.get("cwd") or os.path.expanduser("~")
    cols = int(args.get("cols") or 120)
    rows = int(args.get("rows") or 30)
    with _LOCK:
        old = _SESSIONS.pop(sid, None)
    if old:
        try:
            old["alive"] = False
            old["pty"].terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        pty = PtyProcess.spawn(shell, cwd=cwd, dimensions=(rows, cols))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"spawn failed: {exc}", "error_category": "application"}
    s = {"pty": pty, "output": [], "lock": threading.Lock(), "alive": True,
         "shell": shell, "created": time.time(), "last": time.time(),
         "origin": args.get("origin") or "api"}

    def _reader():
        while s["alive"]:
            try:
                data = pty.read(4096)
                if data:
                    with s["lock"]:
                        s["output"].append(data)
                        if len(s["output"]) > 500:
                            s["output"] = s["output"][-250:]
                else:
                    time.sleep(0.01)
            except EOFError:
                s["alive"] = False
                break
            except Exception:  # noqa: BLE001
                if not s["alive"]:
                    break
                time.sleep(0.01)
    threading.Thread(target=_reader, name=f"term-reader-{sid}", daemon=True).start()
    with _LOCK:
        _SESSIONS[sid] = s
    time.sleep(0.3)
    return {"ok": True, "id": sid, "shell": shell}


def write(args, ctx):
    s = _SESSIONS.get(args.get("id") or "default")
    if not s:
        return {"ok": False, "error": "session not found", "error_category": "application"}
    if not s["alive"]:
        return {"ok": False, "error": "shell died", "error_category": "unavailable"}
    try:
        s["pty"].write(args.get("data") or "")
        _touch(s)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        s["alive"] = False
        return {"ok": False, "error": f"write failed: {exc}", "error_category": "application"}


def read(args, ctx):
    s = _SESSIONS.get(args.get("id") or "default")
    if not s:
        return {"ok": False, "error": "session not found", "error_category": "application"}
    with s["lock"]:
        out = "".join(s["output"])
        s["output"].clear()
    _touch(s)
    return {"ok": True, "output": out, "alive": s["alive"]}


def exec_(args, ctx):
    """Send a command to an interactive session and return output when it goes
    quiescent (v1 behavior). For a reliable exit code, prefer `run`."""
    sid = args.get("id") or "default"
    s = _SESSIONS.get(sid)
    if not s:
        c = create(args, ctx)
        if not c.get("ok"):
            return c
        s = _SESSIONS[sid]
    if not s["alive"]:
        return {"ok": False, "error": "shell died", "error_category": "unavailable"}
    with s["lock"]:
        s["output"].clear()
    cmd = args.get("cmd") or ""
    if cmd:
        try:
            s["pty"].write(cmd + "\r\n")
        except Exception:  # noqa: BLE001
            s["alive"] = False
            return {"ok": False, "error": "shell died", "error_category": "unavailable"}
    time.sleep(0.15)
    timeout = float(args.get("timeout") or 15)
    deadline = time.time() + timeout
    last, stable = 0, 0
    while time.time() < deadline:
        time.sleep(0.1)
        with s["lock"]:
            cur = sum(len(c) for c in s["output"])
        if cur > 0 and cur == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = cur
    with s["lock"]:
        out = "".join(s["output"])
    _touch(s)
    return {"ok": True, "output": out, "alive": s["alive"]}


def resize(args, ctx):
    s = _SESSIONS.get(args.get("id") or "default")
    if not s:
        return {"ok": False, "error": "session not found", "error_category": "application"}
    try:
        s["pty"].setwinsize(int(args.get("rows") or 30), int(args.get("cols") or 120))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "application"}


def _kill(sid):
    with _LOCK:
        s = _SESSIONS.pop(sid, None)
    if s:
        s["alive"] = False
        try:
            s["pty"].terminate()
        except Exception:  # noqa: BLE001
            pass
        return True
    return False


def kill(args, ctx):
    return {"ok": _kill(args.get("id") or "default")}


def list_sessions(args, ctx):
    out = []
    for sid, s in list(_SESSIONS.items()):
        out.append({"id": sid, "alive": s.get("alive", False),
                    "shell": s.get("shell", ""), "origin": s.get("origin", "api"),
                    "uptime": int(time.time() - s.get("created", time.time()))})
    return {"ok": True, "sessions": out}


METHODS = {"run": run, "create": create, "write": write, "read": read,
           "exec": exec_, "resize": resize, "kill": kill, "list": list_sessions}
CAPABILITY = "cap:system.terminal"
VARIANT = None
ATTRS = {"desktop": True}
