"""Public HTTP download service: GET/POST /download, GET /jobs/<id>."""
from __future__ import annotations
import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import unquote, urlsplit, urljoin
import uuid

import aiohttp
from aiohttp import web

ROOT = Path.home() / 'Downloads' / 'ai-pylon2'
TOKEN_FILE = Path.home() / '.config' / 'ai-pylon2' / 'download-token'
MAX_BYTES = 1024 ** 3
STORAGE_BUDGET = 2 * 1024 ** 3
FREE_RESERVE = 512 * 1024 ** 2


def validate_url(url, allow_private=False):
    if not isinstance(url, str) or len(url) > 16384:
        raise ValueError('A valid HTTP(S) URL is required')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only HTTP(S) URLs without embedded credentials are supported')
    if not allow_private:
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if parsed.hostname.lower().rstrip('.') in ('localhost', 'metadata.google.internal'):
                raise ValueError('Private and metadata destinations are not allowed')
        else:
            if not address.is_global:
                raise ValueError('Private and metadata destinations are not allowed')
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=0):
        results = await self.resolver.resolve(host, port, family)
        if any(not ipaddress.ip_address(item['host']).is_global for item in results):
            raise OSError('Private DNS destinations are not allowed')
        return results

    async def close(self):
        await self.resolver.close()


def filename(value):
    if not isinstance(value, str) or not value or value in ('.', '..') or '/' in value or '\\' in value or any(ord(c) < 32 for c in value):
        raise ValueError('filename must be a plain filename')
    if len(value.encode('utf-8')) > 180:
        raise ValueError('filename is too long')
    return value


def _persist(root, job):
    folder = root / job['id']
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = folder / 'job.json.tmp'
    temporary.write_text(json.dumps(job), encoding='utf-8')
    os.replace(temporary, folder / 'job.json')


def _load(root):
    jobs = {}
    for path in root.glob('*/job.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['status'] in ('queued', 'running'):
                job.update(status='interrupted', error='Service restarted; submit again to retry')
                (path.parent / 'payload.part').unlink(missing_ok=True)
                _persist(root, job)
            jobs[job['id']] = job
        except (OSError, ValueError, KeyError):
            continue
    return jobs


async def perform(app, job, options):
    root = app['root']
    temporary = root / job['id'] / 'payload.part'
    try:
        async with app['slots']:
            job.update(status='running', updated=time.time())
            await asyncio.to_thread(_persist, root, job.copy())
            headers = dict(options['headers'])
            url, method = options['url'], options['method']
            body, json_body = options.get('body'), options.get('json')
            deadline = time.monotonic() + options['timeout']
            for redirect in range(6):
                validate_url(url, app['allow_private'])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Download timed out')
                timeout = aiohttp.ClientTimeout(total=remaining, connect=min(30, remaining), sock_read=min(60, remaining))
                async with app['client'].request(method, url, headers=headers, data=body, json=json_body,
                                                allow_redirects=False, timeout=timeout) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        if redirect == 5 or not response.headers.get('Location'):
                            raise ValueError('Too many or invalid redirects')
                        next_url = urljoin(str(response.url), response.headers['Location'])
                        if urlsplit(url)[:2] != urlsplit(next_url)[:2]:
                            headers = {k: v for k, v in headers.items() if k.lower() in ('accept', 'user-agent')}
                        if response.status == 303 or (method == 'POST' and response.status in (301, 302)):
                            method, body, json_body = 'GET', None, None
                            headers = {k: v for k, v in headers.items() if k.lower() != 'content-type'}
                        url = next_url
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise ValueError('Source returned HTTP ' + str(response.status))
                    if response.content_length is not None and response.content_length > options['max_bytes']:
                        raise ValueError('Download exceeds max_bytes')
                    name = options.get('filename')
                    if not name:
                        proposed = response.content_disposition.filename if response.content_disposition else None
                        proposed = (proposed or unquote(urlsplit(str(response.url)).path.rsplit('/', 1)[-1]) or 'download.bin').replace('\\', '/').rsplit('/', 1)[-1]
                        try:
                            name = filename(proposed)
                        except ValueError:
                            name = 'download.bin'
                    if name in ('job.json', 'job.json.tmp', 'payload.part'):
                        raise ValueError('Reserved filename')
                    target = root / job['id'] / name
                    job.update(filename=name, path=str(target), total_bytes=response.content_length,
                               content_type=response.headers.get('Content-Type', ''), source_host=urlsplit(url).hostname)
                    digest = hashlib.sha256()
                    last_save = time.monotonic()
                    stream = await asyncio.to_thread(temporary.open, 'wb')
                    try:
                        async for chunk in response.content.iter_chunked(256 * 1024):
                            job['bytes'] += len(chunk)
                            if job['bytes'] > options['max_bytes']:
                                raise ValueError('Download exceeds max_bytes')
                            used = sum(j['bytes'] for j in app['jobs'].values() if j['status'] in ('running', 'done'))
                            if used > STORAGE_BUDGET:
                                raise ValueError('Download storage quota reached')
                            if time.monotonic() - last_save > 2:
                                free = await asyncio.to_thread(shutil.disk_usage, root)
                                if free.free < FREE_RESERVE:
                                    raise ValueError('Insufficient free disk space')
                            digest.update(chunk)
                            await asyncio.to_thread(stream.write, chunk)
                            if time.monotonic() - last_save > 2:
                                job['updated'] = time.time()
                                await asyncio.to_thread(_persist, root, job.copy())
                                last_save = time.monotonic()
                    finally:
                        await asyncio.to_thread(stream.close)
                    await asyncio.to_thread(os.replace, temporary, target)
                    job.update(status='done', sha256=digest.hexdigest(), updated=time.time())
                    break
    except asyncio.CancelledError:
        job.update(status='interrupted', error='Service stopped; submit again to retry', updated=time.time())
        raise
    except Exception as exc:
        # Never include URL query strings, request headers, or library error dumps.
        message = str(exc) if isinstance(exc, ValueError) else 'Download failed: ' + type(exc).__name__
        job.update(status='error', error=message[:200], updated=time.time())
    finally:
        await asyncio.to_thread(temporary.unlink, missing_ok=True)
        await asyncio.to_thread(_persist, root, job.copy())


def options(data, allow_private=False):
    if not isinstance(data, dict):
        raise ValueError('Send a JSON object')
    url = validate_url(data.get('url'), allow_private)
    headers = data.get('headers', {})
    if not isinstance(headers, dict) or len(headers) > 50:
        raise ValueError('headers must be an object with at most 50 entries')
    for key, value in headers.items():
        if not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or not isinstance(value, str) or any(c in value for c in '\r\n\x00'):
            raise ValueError('Invalid custom header')
        if key.lower() in ('host', 'content-length', 'transfer-encoding', 'connection', 'proxy-authorization'):
            raise ValueError('Unsupported custom header: ' + key)
    method = str(data.get('method', 'GET')).upper()
    if method not in ('GET', 'POST'):
        raise ValueError('Source method must be GET or POST')
    name = filename(data['filename']) if data.get('filename') else None
    limit, timeout = int(data.get('max_bytes', MAX_BYTES)), float(data.get('timeout', 1800))
    if not 1 <= limit <= MAX_BYTES or not 1 <= timeout <= 3600:
        raise ValueError('max_bytes must be 1..1 GiB and timeout 1..3600 seconds')
    body, json_body = data.get('body'), data.get('json')
    if body is not None and not isinstance(body, str):
        raise ValueError('body must be text; use json for a JSON body')
    if body is not None and json_body is not None:
        raise ValueError('Specify body or json, not both')
    return dict(url=url, headers=headers, filename=name, method=method, max_bytes=limit, timeout=timeout, body=body, json=json_body)


@web.middleware
async def authenticate(request, handler):
    if not request.app['public'] and not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + request.app['token']):
        return web.json_response({'ok': False, 'error': 'Unauthorized'}, status=401)
    try:
        return await handler(request)
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError):
        return web.json_response({'ok': False, 'error': 'Invalid request parameters'}, status=400)


async def _submit(request):
    if request.method == 'HEAD':
        return web.json_response({'ok': False, 'error': 'Use GET or POST'}, status=405)
    if len(request.app['jobs']) >= 1000:
        return web.json_response({'ok': False, 'error': 'Job history is full; operator cleanup required'}, status=507)
    if len(request.app['tasks']) >= 32:
        return web.json_response({'ok': False, 'error': 'Download queue is full'}, status=429)
    raw = dict(request.query) if request.method == 'GET' else await request.json()
    config = options(raw, request.app['allow_private'])
    now = time.monotonic()
    rates = request.app['rates']
    for peer in list(rates):
        if now - rates[peer][0] > 60:
            del rates[peer]
    peer = request.remote or 'unknown'
    if len(rates) >= 4096 and peer not in rates:
        return web.json_response({'ok': False, 'error': 'Busy; retry later'}, status=429)
    started, count = rates.get(peer, (now, 0))
    if count >= 10:
        return web.json_response({'ok': False, 'error': 'Limit: 10 downloads per minute per IP'}, status=429)
    rates[peer] = (started, count + 1)
    disk = await asyncio.to_thread(shutil.disk_usage, request.app['root'])
    if disk.free < FREE_RESERVE or sum(j['bytes'] for j in request.app['jobs'].values() if j['status'] in ('running', 'done')) >= STORAGE_BUDGET:
        return web.json_response({'ok': False, 'error': 'Download storage quota reached'}, status=507)
    job = {'id': uuid.uuid4().hex, 'status': 'queued', 'bytes': 0, 'created': time.time(), 'updated': time.time()}
    await asyncio.to_thread(_persist, request.app['root'], job.copy())
    request.app['jobs'][job['id']] = job
    task = asyncio.create_task(perform(request.app, job, config))
    request.app['tasks'].add(task)
    def done(future):
        request.app['tasks'].discard(future)
        if not future.cancelled():
            future.exception()
    task.add_done_callback(done)
    return web.json_response({'ok': True, 'job': job.copy()}, status=202)


async def submit(request):
    async with request.app['submit_lock']:
        return await _submit(request)


async def status(request):
    job = request.app['jobs'].get(request.match_info['id'])
    return web.json_response({'ok': True, 'job': job.copy()} if job else {'ok': False, 'error': 'Unknown download job'}, status=200 if job else 404)


async def resources(app):
    app['root'].mkdir(parents=True, exist_ok=True, mode=0o700)
    app['jobs'] = await asyncio.to_thread(_load, app['root'])
    resolver = None if app['allow_private'] else PublicResolver()
    connector = aiohttp.TCPConnector(resolver=resolver, limit=4)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as client:
        app['client'] = client
        try:
            yield
        finally:
            for task in list(app['tasks']):
                task.cancel()
            await asyncio.gather(*app['tasks'], return_exceptions=True)
    if resolver:
        await resolver.close()


def create_app(root=ROOT, token=None, allow_private=False, public=False):
    app = web.Application(middlewares=[authenticate], client_max_size=65536)
    app.update(root=Path(root), token=token or ('' if public else TOKEN_FILE.read_text().strip()), public=public,
               allow_private=allow_private, tasks=set(), slots=asyncio.Semaphore(2), rates={}, submit_lock=asyncio.Lock())
    app.cleanup_ctx.append(resources)
    app.router.add_get('/download', submit)
    app.router.add_post('/download', submit)
    app.router.add_get('/jobs/{id}', status)
    async def health(request):
        return web.json_response({'ok': True, 'service': 'ai-pylon2-download', 'submit': '/download?url=URL', 'post': {'url': 'https://example.com/file', 'headers': {'User-Agent': 'custom'}}})
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    return app


if __name__ == '__main__':
    web.run_app(create_app(public=True), host='0.0.0.0', port=8080, access_log=None)
