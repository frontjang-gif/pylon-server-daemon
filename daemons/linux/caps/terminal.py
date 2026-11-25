"""Linux command runner and bounded POSIX PTY sessions."""
from __future__ import annotations
import fcntl
import os
import pty
import signal
import struct
import sys
import subprocess
import termios
import threading
import time

CAPABILITY = 'cap:linux.terminal'
_sessions = {}
_lock = threading.RLock()
LIMIT = 1024 * 1024


def _size(fd, args):
    rows, cols = int(args.get('rows', 30)), int(args.get('cols', 120))
    if not (1 <= rows <= 500 and 1 <= cols <= 1000):
        raise ValueError('Invalid terminal dimensions')
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))


def run(args, ctx):
    command = args.get('cmd') or args.get('command')
    if not command:
        raise ValueError('cmd is required')
    requested = float(args.get('timeout', 60))
    timeout = (None if requested <= 0 else requested) if ctx.get('unrestricted') else min(300, max(.1, requested))
    process = subprocess.Popen([args.get('shell') or '/bin/bash', '-lc', command], cwd=args.get('cwd') or None,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    captured = [bytearray(), bytearray()]
    truncated = [False, False]
    def drain(stream, index):
        with stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                room = len(chunk) if ctx.get('unrestricted') else max(0, LIMIT - len(captured[index]))
                captured[index].extend(chunk[:room])
                truncated[index] |= len(chunk) > room
    readers = [threading.Thread(target=drain, args=(stream, i), daemon=True)
               for i, stream in enumerate([process.stdout, process.stderr])]
    started = time.monotonic()
    for thread in readers:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Reap descendants too, including background processes holding output pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for thread in readers:
            thread.join(timeout=5)
    return {'ok': not timed_out, 'stdout': captured[0].decode('utf-8', 'replace'),
            'stderr': captured[1].decode('utf-8', 'replace'), 'exit_code': process.returncode,
            'truncated': any(truncated), 'duration_ms': int((time.monotonic() - started) * 1000),
            **({'error': 'Command timed out', 'error_category': 'timeout'} if timed_out else {})}


def _get(args):
    sid = str(args.get('id') or 'default')
    if sid not in _sessions:
        raise ValueError('No such Linux terminal session')
    record = _sessions[sid]
    record['last'] = time.monotonic()
    return record


def create(args, ctx):
    sid = str(args.get('id') or 'default')
    with _lock:
        for key, record in list(_sessions.items()):
            if time.monotonic() - record['last'] > 1800:
                kill({'id': key}, ctx)
        if sid in _sessions:
            kill({'id': sid}, ctx)
        if len(_sessions) >= 16:
            raise ValueError('Linux terminal session limit reached')
        master, slave = pty.openpty()
        try:
            _size(slave, args)
            # start_new_session is safe from worker threads (no preexec_fn/fork hooks).
            bootstrap = 'import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],[sys.argv[1],"-i"])'
            process = subprocess.Popen([sys.executable, '-c', bootstrap, args.get('shell') or '/bin/bash'], stdin=slave, stdout=slave,
                                       stderr=slave, cwd=args.get('cwd') or None, start_new_session=True,
                                       env=dict(os.environ, TERM='xterm-256color'))
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        _sessions[sid] = {'fd': master, 'process': process, 'last': time.monotonic(), 'created': time.monotonic()}
    return {'ok': True, 'id': sid}


def read(args, ctx):
    with _lock:
        record = _get(args)
        output = bytearray()
        while len(output) < LIMIT:
            try:
                chunk = os.read(record['fd'], min(65536, LIMIT - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
            except (BlockingIOError, OSError):
                break
        return {'ok': True, 'output': output.decode('utf-8', 'replace'), 'alive': record['process'].poll() is None}


def write(args, ctx):
    content = str(args.get('data', '')).encode('utf-8')
    if len(content) > 4096:
        raise ValueError('PTY input must be at most 4096 bytes per write')
    with _lock:
        sent = os.write(_get(args)['fd'], content)
    return {'ok': True, 'written': sent}


def resize(args, ctx):
    with _lock:
        _size(_get(args)['fd'], args)
    return {'ok': True}


def kill(args, ctx):
    with _lock:
        record = _sessions.pop(str(args.get('id') or 'default'), None)
        if record:
            try:
                os.killpg(record['process'].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            record['process'].wait(timeout=5)
            os.close(record['fd'])
    return {'ok': True}


def listing(args, ctx):
    with _lock:
        return {'ok': True, 'sessions': [{'id': sid, 'alive': r['process'].poll() is None,
                                          'uptime': time.monotonic() - r['created']} for sid, r in _sessions.items()]}


def shutdown():
    for sid in list(_sessions):
        kill({'id': sid}, {})


METHODS = {'run': run, 'create': create, 'read': read, 'write': write, 'resize': resize, 'kill': kill, 'list': listing}
