"""capability: cap:desktop.autostart  (Windows — run the stack at user login).

Endpoint-bound: toggles whether the ai-pylon2 stack (supervisor = framework +
daemon) starts automatically when the user logs into Windows, via the standard
per-user HKCU ...\\CurrentVersion\\Run registry value (no admin needed; the v1
tray's "Autostart at Login" equivalent). The command it registers launches
start.bat quietly from the repo root.

Methods:
  status()   -> {ok, enabled, command, repo_root}
  enable()   -> register the Run value; {ok, enabled:true, command}
  disable()  -> remove the Run value;   {ok, enabled:false}
  toggle()   -> flip + return the new state

Degrades to {ok:False, error} off-Windows / if the registry isn't reachable.
"""
from __future__ import annotations

import os

try:
    import winreg
    _WIN = True
except Exception:  # noqa: BLE001
    _WIN = False

_RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
# Same value name as ai-pylon2 on purpose: enabling pylon-server REPLACES an
# ai-pylon2 login entry instead of starting both at login.
_VALUE_NAME = "ai-pylon2"
# pylon-server's Windows launcher (daemon in server mode); ai-pylon2 uses start.bat.
_LAUNCHER = os.path.join("daemons", "windows", "run-server.cmd")


def _repo_root():
    """Repo root = the dir holding start.bat, found by walking up from this cap
    file (daemons/windows/caps/ -> repo root is 3 up). Fall back to cwd."""
    here = os.path.dirname(os.path.abspath(__file__))
    p = here
    for _ in range(6):
        if os.path.isfile(os.path.join(p, "start.bat")) or os.path.isfile(os.path.join(p, _LAUNCHER)):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return os.getcwd()


def _command():
    """The autostart command: launch start.bat --quiet from the repo root in a
    hidden window (cmd /c start "" /min). Quoted for paths with spaces."""
    root = _repo_root()
    launcher = os.path.join(root, _LAUNCHER)
    if os.path.isfile(launcher):        # pylon-server: the daemon in server mode
        return 'cmd /c start "" /min "%s"' % launcher
    bat = os.path.join(root, "start.bat")
    # `start "" /min` launches minimized + detached so login isn't blocked; --quiet
    # sends child output to data\_*.log instead of a console.
    return 'cmd /c start "" /min "%s" --quiet' % bat


def _read():
    """Return the current Run value for our key, or None if absent."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _typ = winreg.QueryValueEx(k, _VALUE_NAME)
            return val
    except FileNotFoundError:
        return None
    except OSError:
        return None


def status(args, ctx):
    if not _WIN:
        return {"ok": False, "error": "autostart is Windows-only", "error_category": "unavailable"}
    cur = _read()
    return {"ok": True, "enabled": cur is not None, "command": cur or _command(),
            "repo_root": _repo_root()}


def enable(args, ctx):
    if not _WIN:
        return {"ok": False, "error": "autostart is Windows-only", "error_category": "unavailable"}
    cmd = _command()
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.SetValueEx(k, _VALUE_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as exc:
        return {"ok": False, "error": "could not write Run key: %s" % exc,
                "error_category": "application"}
    return {"ok": True, "enabled": True, "command": cmd}


def disable(args, ctx):
    if not _WIN:
        return {"ok": False, "error": "autostart is Windows-only", "error_category": "unavailable"}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _VALUE_NAME)
    except FileNotFoundError:
        pass   # already absent → idempotent success
    except OSError as exc:
        # DeleteValue raises FileNotFoundError when the VALUE is missing too;
        # any other OSError is a real failure.
        if getattr(exc, "winerror", None) not in (2,):
            return {"ok": False, "error": "could not remove Run key: %s" % exc,
                    "error_category": "application"}
    return {"ok": True, "enabled": False}


def toggle(args, ctx):
    if not _WIN:
        return {"ok": False, "error": "autostart is Windows-only", "error_category": "unavailable"}
    return disable(args, ctx) if _read() is not None else enable(args, ctx)


METHODS = {"status": status, "enable": enable, "disable": disable, "toggle": toggle}
CAPABILITY = "cap:desktop.autostart"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
