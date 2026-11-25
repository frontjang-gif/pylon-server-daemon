"""Maintain a private reverse SSH forward using the existing auth_ssh app.

No SSH stack or secrets here. Run with --config <runtime JSON>; schedule at
Windows logon for unattended framework-restart/network recovery.
"""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import socket
import time
import urllib.request


# Retry policy. A fixed 15 s retry against an unreachable host (or an sshd that forbids -R) produced ~400 identical
# failures in a morning. Now: exponential backoff (15 s -> 15 min), and a PERMANENT refusal (sshd denies TCP forwarding)
# opens a circuit breaker that only re-probes every ``breaker_seconds`` (default 30 min). The state is written next to
# the log (<config>.state.json) so "why is the bridge down" is one file, not a log hunt.
BASE_DELAY = 15
MAX_DELAY = 900
BREAKER_DELAY = 1800
# while the bridge is UP the loop only re-checks it (a cheap 'reused' reply): every 15 s that was ~5.8k
# auth_ssh/reverse_open calls a day, the 2nd busiest route in the access log (IMPROVEMENTS #1). A healthy check every
# HEARTBEAT seconds; a failure still retries after 15 s and backs off from there.
HEARTBEAT = 60


class Permanent(RuntimeError):
    """The remote refused in a way that retrying does not fix."""


def next_delay(failures, permanent=False, breaker=BREAKER_DELAY):
    """Seconds to wait after the ``failures``-th consecutive failure (0 -> the normal 15 s heartbeat)."""
    if permanent:
        return breaker
    if failures <= 0:
        return BASE_DELAY
    return min(MAX_DELAY, BASE_DELAY * (2 ** min(failures, 10)))


def write_state(path, **state):
    try:
        state['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        Path(path).write_text(json.dumps(state, indent=1), encoding='utf-8')
    except OSError:
        pass


def ensure(config):
    url = config.get('framework_url', 'http://127.0.0.1:8900') + '/v1/apps/platform/auth_ssh/reverse_open'
    args = {'host': config['host'], 'remote_port': config.get('remote_port', 18900),
            'local_port': config.get('local_port', 8900), 'confirm': True}
    request = urllib.request.Request(url, data=json.dumps(args).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.load(response)
    if not result.get('ok'):
        if result.get('permanent'):
            raise Permanent(result.get('error', 'SSH bridge refused'))
        raise RuntimeError(result.get('error', 'SSH bridge unavailable'))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    handler = RotatingFileHandler(config_path.with_suffix('.log'), maxBytes=256000, backupCount=2, encoding='utf-8')
    logging.basicConfig(handlers=[handler], level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Keep one bridge per configured runtime, even across accidental launches.
    guard = socket.socket()
    try:
        guard.bind(('127.0.0.1', int(config.get('guard_port', 38901))))
    except OSError:
        logging.info('Bridge guard is already held; not launching another instance')
        return
    connected = False
    failures = 0
    state_path = config_path.with_suffix('.state.json')
    breaker = int(config.get('breaker_seconds', BREAKER_DELAY))
    while True:
        permanent = False
        try:
            result = ensure(config)
            if not connected or not result.get('reused'):
                logging.info('Bridge ready: remote loopback %s -> local loopback %s', result['remote_port'], result['local_port'])
            connected = True
            if failures:
                logging.info('Bridge recovered after %d failed attempt(s)', failures)
            failures = 0
            write_state(state_path, state='up', remote_port=result.get('remote_port'), failures=0)
        except Exception as exc:
            permanent = isinstance(exc, Permanent)
            if connected or failures == 0 or permanent and failures == 1:
                logging.warning('Bridge interrupted: %s: %s; %s', type(exc).__name__, str(exc)[:200],
                                'refusal is permanent - circuit open, probing every %ds' % breaker if permanent
                                else 'backing off')
            connected = False
            failures += 1
            write_state(state_path, state='circuit_open' if permanent else 'down', failures=failures,
                        error=str(exc)[:300], retry_in=next_delay(failures, permanent, breaker))
        time.sleep(next_delay(failures, permanent, breaker) if failures
                   else int(config.get('heartbeat_seconds', HEARTBEAT)))


if __name__ == '__main__':
    main()
