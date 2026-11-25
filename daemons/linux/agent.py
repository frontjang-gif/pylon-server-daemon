"""Linux endpoint for the existing JSON/WebSocket protocol (docs/PROTOCOL.md)."""
from __future__ import annotations

import asyncio
import getpass
import importlib
import json
import logging
import os
import pkgutil
import random
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from logging_bridge import Relay
import serve_mode  # pylon-server: passive runner or bootstrap of a local server

LOG = logging.getLogger('pylon-linux')


def discover():
    import caps
    found = {}
    for info in pkgutil.iter_modules(caps.__path__):
        if info.name.startswith('_'):
            continue
        module = importlib.import_module('caps.' + info.name)
        found[module.CAPABILITY] = module
    return found


def registration(endpoint, providers):
    meta = {'version': '1', 'user': getpass.getuser(), 'hostname': socket.gethostname()}
    # PYLON2_ENDPOINT_SCOPE=personal declares a RESERVED endpoint: the framework never picks it implicitly for a cap
    # call (only with an explicit hint), whatever its id is - a renamed VM stays reserved (framework/registry._reserved).
    scope = (os.environ.get('PYLON2_ENDPOINT_SCOPE') or '').strip()
    if scope:
        meta['scope'] = scope
        if scope != 'shared':
            meta['reserved'] = True
    return {'type': 'register', 'endpoint': endpoint, 'platform': 'linux',
            'capabilities': [{'name': name, 'variant': 'linux', 'attrs': {'linux': True}}
                             for name in providers] + [{'name': 'cap:_daemon', 'attrs': {'introspection': True}}],
            'meta': meta}


async def dispatch(message, providers, endpoint):
    response = {'type': 'result', 'id': message.get('id')}
    try:
        cap, method = message.get('cap'), message.get('method')
        if cap == 'cap:_daemon' and method in ('versions', 'list_caps'):
            result = {'ok': True, 'endpoint': endpoint, 'pid': os.getpid(),
                      'caps': {name: {'methods': sorted(module.METHODS),
                                      'mtime': Path(module.__file__).stat().st_mtime}
                               for name, module in providers.items()}}
        else:
            provider = providers.get(cap)
            if provider is None or method not in provider.METHODS:
                raise ValueError('Capability or method not provided on this Linux endpoint')
            result = await asyncio.to_thread(provider.METHODS[method], message.get('args') or {}, message.get('ctx') or {})
        if isinstance(result, dict) and result.get('ok') is False:
            return dict(response, ok=False, error=result.get('error', 'Capability failed'),
                        error_category=result.get('error_category', 'application'))
        return dict(response, ok=True, result=result)
    except Exception as exc:
        return dict(response, ok=False, error=str(exc), error_category='application')


async def serve(ws, providers, endpoint):
    await ws.send(json.dumps(registration(endpoint, providers)))
    ack = json.loads(await asyncio.wait_for(ws.recv(), 15))
    if ack.get('type') != 'registered':
        raise ValueError('Framework did not accept registration')
    LOG.info('Registered %s (%s capabilities)', endpoint, len(providers))
    tasks = set()
    limit = asyncio.Semaphore(8)

    async def heartbeat():
        while True:
            await asyncio.sleep(max(1, int(ack.get('heartbeat_interval', 30))))
            await ws.send(json.dumps({'type': 'heartbeat', 'endpoint': endpoint}))

    async def invoke(message):
        async with limit:
            await ws.send(json.dumps(await dispatch(message, providers, endpoint)))

    relay = asyncio.create_task(Relay().run(ws))
    hb = asyncio.create_task(heartbeat())
    try:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict) or message.get('type') != 'invoke':
                continue
            if len(tasks) >= 32:
                await ws.send(json.dumps({'type': 'result', 'id': message.get('id'), 'ok': False,
                                          'error': 'Linux endpoint is busy', 'error_category': 'unavailable'}))
                continue
            task = asyncio.create_task(invoke(message))
            tasks.add(task)
            def finished(done):
                tasks.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
    finally:
        hb.cancel()
        relay.cancel()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(hb, relay, *tasks, return_exceptions=True)


async def main():
    from websockets.asyncio.client import connect
    cfg = {}
    cfg_path = Path(__file__).resolve().parent / 'config.json'
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    url, headers, server = serve_mode.setup(cfg, 'ws://127.0.0.1:18900/agent', log=LOG.info)
    endpoint = os.environ.get('PYLON2_ENDPOINT_ID', 'linux-' + socket.gethostname())
    providers = discover()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    delay = 1
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                async with serve_mode.ws_connect(connect, url, headers, proxy=None, open_timeout=10,
                                                 max_size=4 * 1024 * 1024) as ws:
                    channel = asyncio.create_task(serve(ws, providers, endpoint))
                    stopping = asyncio.create_task(stop.wait())
                    try:
                        await asyncio.wait([channel, stopping], return_when=asyncio.FIRST_COMPLETED)
                        if channel.done():
                            await channel
                    finally:
                        channel.cancel(); stopping.cancel()
                        await asyncio.gather(channel, stopping, return_exceptions=True)
            except Exception as exc:
                LOG.warning('Channel unavailable (%s); reconnecting', type(exc).__name__)
            delay = 1 if time.monotonic() - started > 15 else min(delay * 2, 30)
            try:
                await asyncio.wait_for(stop.wait(), delay + random.random())
            except asyncio.TimeoutError:
                pass
    finally:
        for provider in providers.values():
            if hasattr(provider, 'shutdown'):
                await asyncio.to_thread(provider.shutdown)
        if server:
            await asyncio.to_thread(server.stop)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
