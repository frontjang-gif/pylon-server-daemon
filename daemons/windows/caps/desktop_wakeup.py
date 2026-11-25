"""capability: cap:desktop.wakeup  (Windows — prevent sleep / keep display on).

Endpoint-bound: uses the Win32 power API (SetThreadExecutionState) + a mouse
jiggle fallback to keep this machine (and its display) awake, with physical-idle
detection so it backs off after a deep-idle timeout (lets the box sleep when the
user is genuinely away). Ported from ai-pylon apps/system/wakeup — the ctypes
CORE only; the old web dashboard / screenshot-diff / event-log history are NOT
part of the capability (a page can poll `status` and drive via the methods).

A background keep-awake thread runs while active. Methods:
  status()                 -> {ok, active, jiggle, deep_idle, idle_seconds, ...}
  pause()                  -> stop preventing sleep; release execution state
  resume()                 -> resume preventing sleep
  configure(jiggle?, activity_detection?, deep_idle_timeout?) -> {ok, ...}

Degrades to {ok:False} off-Windows.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

try:
    import ctypes
    from ctypes import wintypes
    _WIN = hasattr(ctypes, "windll")
except Exception:  # noqa: BLE001
    _WIN = False

_LOG = logging.getLogger("system/wakeup")
_LOG.setLevel(logging.INFO)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040


def _send_activity():
    """Insert a balanced pointer nudge using the native, pointer-width-safe ABI."""
    class Mouse(ctypes.Structure):
        _fields_ = [('dx', ctypes.c_int32), ('dy', ctypes.c_int32),
                    ('mouseData', ctypes.c_uint32), ('flags', ctypes.c_uint32),
                    ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]
    class Payload(ctypes.Union):
        _fields_ = [('mouse', Mouse)]  # MOUSEINPUT is the largest INPUT payload.
    class Input(ctypes.Structure):
        _fields_ = [('type', ctypes.c_uint32), ('payload', Payload)]
    events = (Input * 2)()
    for event, dx in zip(events, (1, -1)):
        event.type = 0  # INPUT_MOUSE
        event.payload.mouse.dx = dx
        event.payload.mouse.flags = 0x0001  # MOUSEEVENTF_MOVE
    send = ctypes.windll.user32.SendInput
    send.argtypes = [ctypes.c_uint, ctypes.POINTER(Input), ctypes.c_int]
    send.restype = ctypes.c_uint
    ctypes.windll.kernel32.SetLastError(0)
    inserted = send(2, events, ctypes.sizeof(Input))
    if inserted != 2:
        error = ctypes.windll.kernel32.GetLastError()
        raise OSError(f'SendInput accepted {inserted}/2 activity events (Windows error {error})')
    return inserted


def _interactive_session():
    """Only an actively connected, signed-in Default desktop may be kept awake."""
    try:
        import win32ts
        state = win32ts.WTSQuerySessionInformation(None, -1, win32ts.WTSConnectState)
        if state != win32ts.WTSActive:
            return False, 'session_disconnected'
        if not win32ts.WTSQuerySessionInformation(None, -1, win32ts.WTSUserName):
            return False, 'no_signed_in_user'
        user = ctypes.windll.user32
        user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user.OpenInputDesktop.restype = wintypes.HANDLE
        user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        user.GetUserObjectInformationW.restype = wintypes.BOOL
        user.CloseDesktop.argtypes = [wintypes.HANDLE]
        user.CloseDesktop.restype = wintypes.BOOL
        desktop = user.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not desktop:
            return False, 'locked_or_secure_desktop'
        try:
            name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
            if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                return False, 'desktop_query_failed'
            if name.value.casefold() != 'default':
                return False, 'locked_or_secure_desktop'
        finally:
            user.CloseDesktop(desktop)
        return True, None
    except Exception:
        # An unknown session is never permission to keep the sign-in screen awake.
        return False, 'session_query_failed'


def _detect_modern_standby():
    """Detect Modern Standby for diagnostics only; never changes the power plan."""
    if not _WIN:
        return False
    try:
        import subprocess
        out = subprocess.run(["powercfg", "/a"], capture_output=True, text=True,
                             timeout=8).stdout.lower()
        return "s0 low power idle" in out
    except Exception:  # noqa: BLE001
        return False


class _Wakeup:
    def __init__(self):
        self.active = True
        self.jiggle = True
        self.activity_detection = True
        self.deep_idle_timeout = 3600  # seconds idle -> allow sleep
        self.keep_awake_interval = 25
        # #WAKE: jiggle well UNDER a typical display-off timeout (often 5 min) so
        # a missed/late tick can't let the screen sleep. 25s gives a comfortable
        # margin; configurable via configure(jiggle_interval=).
        self.jiggle_interval = 25
        self._deep_idle = False
        self._session_interactive = False
        self._suspended_reason = 'checking_session'
        self._started_at = datetime.now().isoformat()
        self._stats = {"keep_awake_calls": 0, "jiggle_calls": 0, "keep_awake_failures": 0, "activity_failures": 0}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self.duration_seconds = 3600
        self._deadline = time.monotonic() + self.duration_seconds
        self._expired = False
        self._expiry_input_tick = None
        self._auto_resume_count = 0
        self._last_error = None
        self._synthetic_tick = None
        self._physical_tick = None
        self._thread = None
        self._last_jiggle = 0.0
        self._last_status_log = 0.0
        self._last_status_state = None
        self._last_input_verified = None
        self._input_last_error = None
        self._activity_last_success = None
        self._last_activity_attempt = 0.0
        # #1: whether the Win32 power API actually HOLDS the DISPLAY request on
        # this box. None = not yet probed; True/False set by _probe_power_api().
        # On a box whose corporate power policy ignores SetThreadExecutionState
        # this is False, which the UI surfaces as "policy-blocked — the jiggle is
        # doing the real work; a hard fix needs admin powercfg".
        self._power_api_effective = None
        # Retained only to release an older in-memory display override during upgrade.
        # New keep-awake sessions never change the machine-wide power scheme.
        self._saved_monitor_timeouts = None       # (ac_secs, dc_secs) once stashed
        self._display_policy_applied = False
        self._modern_standby = _detect_modern_standby()

    def start(self):
        with self._lock:
            self._start_locked()

    def _start_locked(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="wakeup-keepawake", daemon=True)
        self._thread.start()

    def _input_tick(self):
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            raise OSError('GetLastInputInfo failed')
        return info.dwTime

    def _user_idle_seconds(self):
        tick = self._input_tick()
        if self._physical_tick is None or tick != self._synthetic_tick:
            self._physical_tick = tick
        return ((ctypes.windll.kernel32.GetTickCount() - self._physical_tick) & 0xFFFFFFFF) / 1000.0

    def _keep_awake_tick(self):
        if not self.active:
            return
        idle = self._user_idle_seconds()
        if self.activity_detection and idle >= self.deep_idle_timeout:
            if not self._deep_idle:
                self._deep_idle = True
                ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(ES_CONTINUOUS))
                self.restore_display_policy()
            return
        self._deep_idle = False
        # Keep the SYSTEM + DISPLAY awake. NOTE: do NOT include
        # ES_AWAYMODE_REQUIRED here — it's for background media servers, and on
        # this/modern Windows bundling it makes SetThreadExecutionState NOT hold
        # the DISPLAY flag (verified: the call returns a prior-state of only
        # ES_CONTINUOUS, i.e. the display request was dropped), so the screen
        # STILL SLEEPS. The standard ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        # combo holds correctly. Must be reasserted each tick from THIS (alive)
        # thread, which the loop does. A 0 return means the call FAILED — count
        # it so a silent failure is visible in status (don't bump the success
        # counter in that case).
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        r = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
        if r:
            self._stats["keep_awake_calls"] += 1
            # #1: r is the PRIOR execution-state. After the first tick we have
            # been continuously requesting DISPLAY, so a subsequent tick's prior
            # state SHOULD carry ES_DISPLAY_REQUIRED if the OS is honouring our
            # request. If the prior state never shows the DISPLAY bit (only
            # ES_CONTINUOUS), a power/presence policy is stripping it → the power
            # API is NOT effective here and the jiggle is the real mechanism.
            prior = int(r) & 0xFFFFFFFF
            if self._stats["keep_awake_calls"] >= 2:
                self._power_api_effective = bool(prior & ES_DISPLAY_REQUIRED)
        else:
            self._stats["keep_awake_failures"] = self._stats.get("keep_awake_failures", 0) + 1
            self._power_api_effective = False

    def _jiggle_tick(self):
        if not (self.active and self.jiggle and not self._deep_idle):
            return
        now = time.monotonic()
        interval = self.jiggle_interval if self._input_last_error is None else 5
        if now - self._last_activity_attempt < interval:
            return
        self._last_activity_attempt = now
        try:
            self._user_idle_seconds()  # Keep genuine-user idle separate from our input.
            before = self._input_tick()
            time.sleep(0.02)  # Avoid matching a genuine input's millisecond timestamp.
            inserted = _send_activity()
            for _ in range(10):
                tick = self._input_tick()
                if tick != before:
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError('Activity events were accepted but Windows input-idle did not reset')
            self._synthetic_tick = tick
            self._last_input_verified = True
            self._input_last_error = None
            self._activity_last_success = time.time()
            self._stats["jiggle_calls"] += 1
            self._last_jiggle = now
            idle = ((ctypes.windll.kernel32.GetTickCount() - tick) & 0xFFFFFFFF) / 1000.0
            _LOG.info('Keep-awake activity verified', extra={'fields': {
                'input_events': inserted, 'input_idle_seconds': idle,
                'activity_count': self._stats['jiggle_calls'], 'interval_seconds': self.jiggle_interval}})
        except Exception as exc:
            self._last_input_verified = False
            self._input_last_error = str(exc)
            self._stats['activity_failures'] += 1
            _LOG.warning('Keep-awake activity failed', extra={'fields': {
                'error': self._input_last_error, 'retry_seconds': 5}})

    def _step(self):
        """Only the keep-awake worker calls this: power requests are per-thread."""
        with self._lock:
            if self.active and self._deadline is not None and time.monotonic() >= self._deadline:
                self.active = False
                self._expired = True
                self._deadline = None
                self._expiry_input_tick = self._input_tick()
                _LOG.info('Keep-awake timer expired; waiting for user activity')
            self._session_interactive, self._suspended_reason = _interactive_session()
            if self._expired and self._session_interactive:
                tick = self._input_tick()
                if self._expiry_input_tick is None:
                    self._expiry_input_tick = tick
                elif tick != self._expiry_input_tick and tick != self._synthetic_tick:
                    self.active = True
                    self._expired = False
                    self._expiry_input_tick = None
                    self._deadline = time.monotonic() + self.duration_seconds if self.duration_seconds else None
                    self._auto_resume_count += 1
                    _LOG.info('Keep-awake restarted after user activity', extra={'fields': {
                        'duration_seconds': self.duration_seconds, 'auto_resume_count': self._auto_resume_count}})
            if self.active and self._session_interactive:
                self._keep_awake_tick()
                self._jiggle_tick()
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(ES_CONTINUOUS))
                self._deep_idle = False
                self._last_input_verified = None
                self.restore_display_policy()

    def _loop(self):
        while True:
            self._wake.clear()
            try:
                self._step()
                self._last_error = None
                self._log_status()
            except Exception as exc:
                if self._last_error != str(exc):
                    import logging
                    logging.getLogger("system/wakeup").exception("Keep-awake worker failed")
                self._last_error = str(exc)
                try:
                    self._log_status()
                except Exception:
                    pass
            # Expiry/pause respond within a second, even if the page is closed.
            self._wake.wait(1)

    def _log_status(self):
        now = time.monotonic()
        state = (self.active, self._expired, self._deep_idle, self._last_error,
                 self._last_input_verified, self._input_last_error, self._display_policy_applied,
                 self._session_interactive, self._suspended_reason)
        if state != self._last_status_state or now - self._last_status_log >= 60:
            self._last_status_state, self._last_status_log = state, now
            _LOG.info("Keep-awake status", extra={'fields': self.snapshot()})

    def set_active(self, active, duration=None):
        with self._lock:
            if duration is not None:
                self.duration_seconds = duration
            self.active = active
            self._expired = False
            self._expiry_input_tick = None
            self._deadline = (time.monotonic() + self.duration_seconds
                              if active and self.duration_seconds else None)
            self._deep_idle = False
            self.start()
            self._wake.set()

    # Legacy display-policy restoration helpers. New sessions never apply
    # a global timeout override: it could survive sign-out or a process crash.
    def _powercfg_query_monitor_timeouts(self):
        """Return (ac_secs, dc_secs) current 'turn off display after' indices, or
        (None, None) on any failure. Index is in SECONDS."""
        import subprocess
        try:
            out = subprocess.run(
                ["powercfg", "/query", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE"],
                capture_output=True, text=True, timeout=8).stdout
        except Exception:  # noqa: BLE001
            return (None, None)
        ac = dc = None
        for line in out.splitlines():
            s = line.strip().lower()
            if s.startswith("current ac power setting index"):
                try:
                    ac = int(s.split(":")[-1].strip(), 16)
                except Exception:  # noqa: BLE001
                    pass
            elif s.startswith("current dc power setting index"):
                try:
                    dc = int(s.split(":")[-1].strip(), 16)
                except Exception:  # noqa: BLE001
                    pass
        return (ac, dc)

    def _powercfg_set_monitor_timeout(self, ac_secs, dc_secs):
        """Set the active scheme's monitor timeout (seconds; 0 = never). Per-user,
        no admin. Best-effort — returns True on success."""
        import subprocess
        ok = True
        for flag, secs in (("monitor-timeout-ac", ac_secs), ("monitor-timeout-dc", dc_secs)):
            if secs is None:
                continue
            try:
                # powercfg /change takes MINUTES; our stored values are seconds.
                mode = "/setacvalueindex" if flag.endswith("-ac") else "/setdcvalueindex"
                result = subprocess.run(["powercfg", mode, "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE", str(int(secs))],
                                        capture_output=True, text=True, timeout=8)
                ok = result.returncode == 0 and ok
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            result = subprocess.run(["powercfg", "/setactive", "SCHEME_CURRENT"],
                                    capture_output=True, text=True, timeout=8)
            ok = result.returncode == 0
        return ok

    def restore_display_policy(self):
        """Restore the user's prior monitor timeouts (called on pause). No-op if
        we never applied / never stashed."""
        if not _WIN or not self._display_policy_applied:
            return
        saved = self._saved_monitor_timeouts
        if saved is not None:
            ac, dc = saved
            # powercfg /change is minutes; convert back from our stored seconds
            if not self._powercfg_set_monitor_timeout(ac, dc):
                raise RuntimeError('Windows rejected restoring display timeouts')
        self._display_policy_applied = False
        self._saved_monitor_timeouts = None

    def snapshot(self):
        eff = self._power_api_effective
        return {"ok": True, "active": self.active and self._session_interactive and not self._deep_idle,
                "enabled": self.active, "session_interactive": self._session_interactive,
                "suspended_reason": self._suspended_reason if self.active else None,
                "duration_seconds": self.duration_seconds,
                "remaining_seconds": (max(0, round(self._deadline - time.monotonic())) if self._deadline is not None else None),
                "input_idle_reset_verified": self._last_input_verified,
                "activity_last_success": self._activity_last_success,
                "input_last_error": self._input_last_error,
                "expired": self._expired, "last_error": self._last_error,
                "auto_resume_on_activity": True, "auto_resume_count": self._auto_resume_count,
                "worker_running": bool(self._thread and self._thread.is_alive()), "jiggle": self.jiggle,
                "activity_detection": self.activity_detection,
                "deep_idle": self._deep_idle, "deep_idle_timeout": self.deep_idle_timeout,
                "jiggle_interval": self.jiggle_interval,
                # #1: whether SetThreadExecutionState actually holds the DISPLAY
                # request on this box. False => a corporate power policy is
                # ignoring it; the mouse+keyboard jiggle is doing the real work
                # and a guaranteed fix needs admin `powercfg`. None => not yet
                # probed (needs ≥2 keep-awake ticks).
                "power_api_effective": eff,
                "modern_standby": self._modern_standby,
                "display_policy_applied": self._display_policy_applied,
                "display_policy_note": "Windows display timeouts unchanged; protection uses session-scoped power requests and input activity",
                "power_api_note": (
                    None if eff is None else
                    ("power API honoured (display stays on via SetThreadExecutionState)"
                     if eff else
                     "power API ignored by policy — relying on input jiggle; a guaranteed "
                     "fix needs admin powercfg (set console/display timeout to 0 on AC)")),
                "idle_seconds": round(self._user_idle_seconds(), 1) if _WIN and self._session_interactive else None,
                "started_at": self._started_at, "stats": dict(self._stats)}


_previous_worker = globals().get('_W')
if _WIN and _previous_worker is not None:
    # Hot reload upgrades the live worker without creating a second input thread
    # or replacing the user's paused/expired/timed state.
    _previous_worker.__class__ = _Wakeup
    if not hasattr(_previous_worker, '_expiry_input_tick'):_previous_worker._expiry_input_tick = None
    if not hasattr(_previous_worker, '_auto_resume_count'):_previous_worker._auto_resume_count = 0
    _W = _previous_worker
else:
    _W = _Wakeup() if _WIN else None
def start_background():
    """Called by the daemon lifecycle, independent of page visits."""
    if _W is not None:
        _W.start()


def _na():
    return {"ok": False, "error": "wakeup is Windows-only on this endpoint",
            "error_category": "unavailable"}


def status(args, ctx):
    if not _WIN:
        return _na()
    _W.start()
    return _W.snapshot()


def pause(args, ctx):
    if not _WIN:
        return _na()
    _W.set_active(False)
    return _W.snapshot()


def resume(args, ctx):
    if not _WIN:
        return _na()
    try:
        duration = int(args.get("duration_seconds", 3600))
        if duration < 0 or duration > 604800:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "duration_seconds must be 0 (unlimited) or 1..604800"}
    _W.set_active(True, duration)
    return _W.snapshot()


def configure(args, ctx):
    if not _WIN:
        return _na()
    if "jiggle" in args:
        _W.jiggle = bool(args["jiggle"])
    if "activity_detection" in args:
        _W.activity_detection = bool(args["activity_detection"])
    if "deep_idle_timeout" in args:
        try:
            _W.deep_idle_timeout = max(60, int(args["deep_idle_timeout"]))
        except (TypeError, ValueError):
            pass
    if "jiggle_interval" in args:
        try:
            _W.jiggle_interval = max(5, int(args["jiggle_interval"]))
        except (TypeError, ValueError):
            pass
    _W.start()
    _W._wake.set()
    return _W.snapshot()


METHODS = {"status": status, "pause": pause, "resume": resume, "configure": configure}
CAPABILITY = "cap:desktop.wakeup"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
