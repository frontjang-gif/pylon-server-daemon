"""_toast — clickable Windows toast notifications with BUTTONS (windows-toasts).

NOT a capability (``_``-prefixed). Used by the tray host (_tray_host.py) to show
ACTIONABLE notifications — e.g. a device-code sign-in — as a real Windows
Action-Center toast with working buttons, instead of the legacy pystray balloon
(which has no buttons and isn't clickable).

Uses ``InteractableWindowsToaster`` so button clicks activate an IN-PROCESS
callback, with a registered per-user ai-pylon2 AUMID. Buttons:
  * "Open"       -> opens the notification's URL in the browser.
  * "Copy code"  -> copies a device/sign-in code to the clipboard (when present).
  * CUSTOM       -> pass buttons=[{label, op?, url?, confirm?}] for arbitrary
                    action buttons (Windows-Update style: Restart now / Pick a
                    time / Restart tonight); an `op` button POSTs a framework op.
  * STICKY       -> scenario='reminder'/'alarm' keeps the toast until the user
                    interacts (not auto-closed) + persists it in the Action Center.
Clicking the toast body also opens the URL.

available() is False (and show() returns False) when windows-toasts / WinRT isn't
importable, so the tray host falls back to the pystray balloon. Never raises.
"""
from __future__ import annotations

import os
import threading
import webbrowser

try:                                        # imported as a package module (tests, tooling)
    from . import _urlutil
except ImportError:                         # the tray host runs this file as a sibling top-level script
    import _urlutil

APP_ID = "AIPylon2.Desktop"
APP_NAME = "ai-pylon2"

_toaster = None
_lock = threading.Lock()
# #14: last-shown content per tag — a tagged sticky refresh with identical
# content is a no-op (don't re-fire the toast), and the shared tag makes a new
# toast replace the prior one in the Action Center instead of stacking.
_LAST_TOAST = {}


def available():
    try:
        import windows_toasts  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _register_identity():
    """Use windows-toasts' documented per-user AUMID registration (no admin)."""
    import winreg
    path = "Software\\Classes\\AppUserModelId\\" + APP_ID
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, APP_NAME)


def _get_toaster():
    global _toaster
    with _lock:
        if _toaster is None:
            from windows_toasts import InteractableWindowsToaster
            # windows-toasts requires a registered identity for custom titles and
            # working actions. Register for this user before constructing WinRT.
            _register_identity()
            _toaster = InteractableWindowsToaster(APP_NAME, notifierAUMID=APP_ID)
        return _toaster


def prewarm(background=True):
    """Build the InteractableWindowsToaster ahead of the first real toast.

    Constructing the toaster triggers a WinRT cold init that otherwise lands on
    the FIRST actionable notification — often the sign-in prompt, where latency
    is most visible. Calling this at tray-host start pays that cost up front, in
    the background, so the first `show()` is already warm. Safe + idempotent:
    no-op when windows-toasts isn't importable, never raises into the caller."""
    def _do():
        try:
            _get_toaster()
        except Exception:  # noqa: BLE001
            pass
    if not available():
        return False
    if background:
        threading.Thread(target=_do, name="toast-prewarm", daemon=True).start()
    else:
        _do()
    return True


def _open_url(url, fw_url=""):
    url = _urlutil.absolute(url, fw_url)       # a page-relative link needs the server part to be openable
    if not url:
        return
    try:
        os.startfile(url)   # noqa: S606 — our own/sign-in URL
    except Exception:  # noqa: BLE001
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass


def _copy(text):
    if not text:
        return
    try:
        import subprocess
        # clip.exe reads stdin → clipboard; robust, no extra deps.
        p = subprocess.Popen(["clip"], stdin=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p.communicate(input=text.encode("utf-16-le"))
    except Exception:  # noqa: BLE001
        pass


def _post_op(fw_url, op, confirm=False):
    """POST a framework op from a toast-button click (the back-channel). op is a
    path like 'system/wakeup/pause'. Best-effort."""
    if not (fw_url and op):
        return
    try:
        import json as _json
        import urllib.request
        body = _json.dumps({"confirm": True} if confirm else {}).encode("utf-8")
        req = urllib.request.Request(
            fw_url.rstrip("/") + "/v1/apps/" + str(op).strip().strip("/"),
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass


def show(title, message, *, url="", code="", buttons=None, scenario="", fw_url="", tag=""):
    """Show a clickable toast. Returns True if a toast was shown.

    buttons: optional list of custom action buttons (like the Windows Update
      toast's Restart now / Pick a time / Restart tonight). Each is a dict:
        {label, op?, url?, confirm?}
      - op  → clicking POSTs that framework op (needs fw_url; confirm adds
              confirm:true)
      - url → clicking opens that url
      When buttons is given it REPLACES the default Open/Copy-code buttons.
    scenario: '' (default, auto-dismisses) | 'reminder' | 'urgent'/'important' |
      'alarm'. 'reminder'/'alarm' make the toast STAY until the user interacts
      (not auto-closed) + persist in the Action Center — the Windows-Update
      style. Raises on a real failure so the caller can log WHY.
    tag: optional stable identifier (#14). A rapidly-updating STICKY (e.g. an MFA
      number refreshed via upsert) passes the SAME tag each refresh so the new
      toast REPLACES the prior one in the Action Center (tag/group) instead of
      stacking N toasts — and we skip re-showing when the content is byte-for-byte
      unchanged. Without a tag every show() is a fresh toast (prior behaviour).
    """
    from windows_toasts import Toast, ToastButton, ToastScenario
    toaster = _get_toaster()

    # #14: a tagged refresh whose content is unchanged is a no-op (a sticky
    # upsert that re-posts the same number shouldn't re-fire the toast).
    if tag:
        sig = (str(title or ""), str(message or ""), str(url or ""), str(scenario or ""))
        with _lock:
            if _LAST_TOAST.get(tag) == sig:
                return True
            _LAST_TOAST[tag] = sig

    toast = Toast(text_fields=[str(title or "ai-pylon2"), str(message or "")])

    # #14: tag/group so repeated shows with the same tag REPLACE in place.
    if tag:
        try:
            toast.tag = str(tag)
            toast.group = "ai-pylon2"
        except Exception:  # noqa: BLE001 — older windows-toasts w/o tag support
            pass

    # stay-until-interaction scenarios (Windows Update style).
    _scn = {"reminder": ToastScenario.Reminder, "alarm": ToastScenario.Alarm,
            "urgent": ToastScenario.Important, "important": ToastScenario.Important}
    scn = _scn.get(str(scenario or "").strip().lower())
    if scn is not None:
        toast.scenario = scn

    # custom action buttons (arg = "op:<op>" / "openurl:<url>"), else the legacy
    # Open / Copy-code defaults.
    routes = {}
    btns = buttons if isinstance(buttons, list) and buttons else None
    if btns:
        for i, b in enumerate(btns):
            if not isinstance(b, dict):
                continue
            label = str(b.get("label") or "").strip()
            if not label:
                continue
            arg = "btn:%d" % i
            routes[arg] = b
            toast.AddAction(ToastButton(label, arg))
    else:
        if url:
            toast.AddAction(ToastButton("Open", "open"))
        if code:
            toast.AddAction(ToastButton("Copy code", "copy"))

    def _on_activated(args):
        try:
            arg = getattr(args, "arguments", "") or ""
        except Exception:  # noqa: BLE001
            arg = ""
        # custom button?
        b = routes.get(arg)
        if b is not None:
            if b.get("op"):
                _post_op(fw_url, b.get("op"), confirm=bool(b.get("confirm")))
            elif b.get("url"):
                _open_url(b.get("url"), fw_url)
            return
        # legacy / body click
        if arg == "copy":
            _copy(code)
        else:
            _open_url(url, fw_url)

    toast.on_activated = _on_activated
    toaster.show_toast(toast)
    return True
