"""Daemon modes: a PASSIVE capability runner, or the BOOTSTRAP of a local server.

Shared by daemons/<os>/agent.py (stdlib only).

passive (default)   connect to the configured framework URL (a remote
                    pylon-server: the VM, an ai-web pod, or a local one) and
                    serve capabilities. Sends ``Authorization: Bearer <agent
                    token>`` when one is configured.
server              start pylon-server from the checkout this daemon lives in
                    (``python -m uvicorn main:app``), keep it running, and
                    connect to it as this machine's capability endpoint. A fresh
                    agent token is made for each start; the server only gets its
                    SHA-256 (``PYLON_AGENT_TOKEN_SHA256``).

Settings (env wins over the daemon's config.json):
    PYLON2_DAEMON_MODE / "mode"            passive | server
    PYLON2_FRAMEWORK_URL / "framework_url" passive: ws(s)://host[/prefix]/agent
    PYLON2_AGENT_TOKEN                     passive: the agent token itself
    "agent_token_file"                     passive: file holding it
                                           (default ~/.config/pylon-server/agent-token)
    PYLON2_SERVER_HOST / "server_host"     server: bind address (default 127.0.0.1)
    PYLON2_SERVER_PORT / "server_port"     server: port (default 8910)
    "server_env"                           server: extra env for the server (dict)
    "local_owner"                          server: true = requests made on this machine are
                                           the owner, no sign-in (loopback-bound server only)
"""
import hashlib
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = Path.home() / '.config' / 'pylon-server' / 'agent-token'


def mode(cfg):
    m = (os.environ.get('PYLON2_DAEMON_MODE') or cfg.get('mode') or 'passive').strip().lower()
    if m not in ('passive', 'server'):
        raise SystemExit('daemon mode must be passive or server, not %r' % m)
    return m


def passive_token(cfg):
    tok = os.environ.get('PYLON2_AGENT_TOKEN', '').strip()
    if tok:
        return tok
    path = Path(cfg.get('agent_token_file') or DEFAULT_TOKEN_FILE).expanduser()
    try:
        return path.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def auth_headers(token):
    return {'Authorization': 'Bearer ' + token} if token else {}


def ws_connect(connect, url, headers, **kw):
    """websockets.connect across versions: >= 14 (and the 13 asyncio client) take
    additional_headers, the legacy client of <= 13 takes extra_headers. The
    legacy client swallows unknown keywords and only fails on connect, so the
    parameter is chosen from the signature, not by trial."""
    import inspect
    if not headers:
        return connect(url, **kw)
    try:
        params = inspect.signature(connect).parameters
    except (TypeError, ValueError):
        params = {}
    key = 'additional_headers' if 'additional_headers' in params else 'extra_headers'
    return connect(url, **{key: headers}, **kw)


def _die_with_parent():
    """Linux: the server gets SIGTERM if this daemon dies, even by SIGKILL."""
    try:
        import ctypes
        import signal
        ctypes.CDLL('libc.so.6', use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except Exception:
        pass


def _windows_kill_on_close_job():
    """Windows: a Job Object that kills every process in it when this daemon's
    handle closes (the daemon exits or is killed). The server's own children
    (framework, services) join the job automatically."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class BASIC(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ('r', 'w', 'o', 'rb', 'wb', 'ob')]

    class EXTENDED(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    return job


def _windows_assign(job, proc):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x0101, False, proc.pid)             # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if h:
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(h))
        k32.CloseHandle(wintypes.HANDLE(h))


class LocalServer:
    """Runs and supervises ``uvicorn main:app`` from the repository root."""

    def __init__(self, cfg, log=print):
        raw_root = os.environ.get('PYLON2_SERVER_ROOT') or cfg.get('server_root')
        if not raw_root:
            raise ValueError('Server mode requires an explicit PYLON2_SERVER_ROOT or server_root')
        self.server_root = Path(raw_root).expanduser().resolve()
        if not (self.server_root / 'main.py').is_file() or not (self.server_root / 'composition.json').is_file():
            raise ValueError('Server root must be a composed server release')
        self.server_python = Path(os.environ.get('PYLON2_SERVER_PYTHON') or cfg.get('server_python') or sys.executable).expanduser().resolve()
        if not self.server_python.is_file():
            raise ValueError('Configured server Python executable not found')
        self.host = os.environ.get('PYLON2_SERVER_HOST') or cfg.get('server_host') or '127.0.0.1'
        self.port = int(os.environ.get('PYLON2_SERVER_PORT') or cfg.get('server_port') or 8910)
        self.extra_env = dict(cfg.get('server_env') or {})
        # A desktop install: whoever sits at this machine is the owner (no sign-in).
        # Only for a loopback-bound server; the bootstrap adds its own checks.
        if cfg.get('local_owner') and self.host in ('127.0.0.1', 'localhost', '::1'):
            self.extra_env.setdefault('PYLON_LOCAL_OWNER', '1')
        self.token = secrets.token_urlsafe(32)
        self.log = log
        self.proc = None
        self._stop = threading.Event()
        self._job = None
        if os.name == 'nt':
            try:
                self._job = _windows_kill_on_close_job()
            except Exception:
                self._job = None
        self.restarts = 0

    @property
    def url(self):
        return 'ws://127.0.0.1:%d/agent' % self.port

    @property
    def http_url(self):
        return 'http://127.0.0.1:%d' % self.port

    def _env(self):
        env = {**os.environ, **{k: str(v) for k, v in self.extra_env.items()},
               'PYLON_AGENT_TOKEN_SHA256': hashlib.sha256(self.token.encode()).hexdigest(),
               'PYTHONUNBUFFERED': '1'}
        env.pop('PYLON2_AGENT_TOKEN', None)
        env.pop('PYTHONPATH', None)
        return env

    def _spawn(self):
        argv = [str(self.server_python), '-m', 'uvicorn', 'main:app', '--host', self.host, '--port', str(self.port)]
        extra = {'preexec_fn': _die_with_parent} if sys.platform.startswith('linux') else {}
        self.proc = subprocess.Popen(argv, cwd=str(self.server_root), env=self._env(), stdin=subprocess.DEVNULL, **extra)
        if self._job:
            try:
                _windows_assign(self._job, self.proc)
            except Exception:
                pass
        self.log('[serve] started pylon-server pid %d on %s:%d' % (self.proc.pid, self.host, self.port))

    def healthy(self, timeout=2):
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/health' % self.port, timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def start(self, wait_s=90):
        if self.healthy(1):
            raise SystemExit('port %d already serves something; set server_port' % self.port)
        self._spawn()
        deadline = time.time() + wait_s
        while time.time() < deadline and not self.healthy(1):
            if self.proc.poll() is not None:
                raise SystemExit('pylon-server exited during start (code %s)' % self.proc.returncode)
            time.sleep(0.5)
        threading.Thread(target=self._supervise, name='pylon-server-supervisor', daemon=True).start()
        return self

    def _supervise(self):
        backoff = 1
        while not self._stop.wait(2):
            if self.proc.poll() is None:
                backoff = 1
                continue
            self.log('[serve] pylon-server exited (code %s); restarting in %ds' % (self.proc.returncode, backoff))
            if self._stop.wait(backoff):
                return
            self.restarts += 1
            self._spawn()
            backoff = min(backoff * 2, 60)

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def setup(cfg, default_url, log=print):
    """-> (framework_url, headers, LocalServer or None) for this daemon."""
    if mode(cfg) == 'server':
        server = LocalServer(cfg, log).start()
        log('[serve] server mode: this daemon is the bootstrap of %s' % server.server_root)
        return server.url, auth_headers(server.token), server
    url = os.environ.get('PYLON2_FRAMEWORK_URL') or cfg.get('framework_url') or default_url
    return url, auth_headers(passive_token(cfg)), None
