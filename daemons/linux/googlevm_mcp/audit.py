"""Correlate authenticated MCP requests and record every tool outcome."""
import asyncio
from contextvars import ContextVar
import json
import uuid
import time

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    SDK2 = True
except ImportError:  # Development environments still using the legacy SDK.
    from mcp.server.fastmcp import FastMCP
    SDK2 = False
from mcp.server.auth.middleware.auth_context import get_access_token

CURRENT_SESSION = ContextVar('googlevm_session', default='')


def principal():
    token = get_access_token()
    return token.client_id if token else 'local-test'


class RecordedMCP(FastMCP):
    def __init__(self, *args, records, **kwargs):
        self.records = records
        super().__init__(*args, **kwargs)

    async def call_tool(self, name, arguments, context=None):
        sid = CURRENT_SESSION.get() or await asyncio.to_thread(self.records.open_session, principal(), automatic=True)
        correlation = uuid.uuid4().hex
        await asyncio.to_thread(self.records.event, sid, 'tool.request',
                                {'call_id': correlation, 'name': name, 'arguments': arguments})
        try:
            result = await super().call_tool(name, arguments, context=context) if SDK2 else await super().call_tool(name, arguments)
        except BaseException as exc:
            await asyncio.to_thread(self.records.event, sid, 'tool.error',
                                    {'call_id': correlation, 'name': name, 'error': type(exc).__name__ + ': ' + str(exc)})
            if SDK2 and isinstance(exc, UnexpectedToolError) and isinstance(exc.__cause__, (ValueError, FileNotFoundError, PermissionError)):
                raise ToolError(str(exc.__cause__)) from exc
            raise
        # Do not recursively embed session snapshots inside themselves.
        saved = {'history_reference': True, 'session_id': arguments.get('session_id')} if name in ('vm.session.get', 'vm.session.list') else result
        await asyncio.to_thread(self.records.event, sid, 'tool.result', {'call_id': correlation, 'name': name, 'result': saved})
        return result


def safe_headers(headers):
    # Preserve order and duplicates, redact authentication/cookie values only.
    return [[name.decode('latin1'), '[redacted]' if name.lower() in
             (b'authorization', b'proxy-authorization', b'cookie', b'set-cookie') else value.decode('latin1')]
            for name, value in headers]


class SessionMiddleware:
    def __init__(self, app, records, provider, max_bytes=2*1024*1024):
        self.max_bytes = max_bytes
        self.app, self.records, self.provider = app, records, provider

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path', '').rstrip('/') != '/mcp':
            return await self.app(scope, receive, send)
        started = time.monotonic()
        headers = dict(scope.get('headers', []))
        scheme, _, token = headers.get(b'authorization', b'').decode('latin1').partition(' ')
        owner = await asyncio.to_thread(self.provider.authenticate, token) if scheme.lower() == 'bearer' else None
        client = owner.removeprefix('oauth:') if owner else 'http-unauthenticated'
        sid = await asyncio.to_thread(self.records.open_session, client, automatic=True)
        body = bytearray()
        disconnected, too_large = False, False
        if scope.get('method') == 'POST':
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    disconnected = True
                    break
                body.extend(message.get('body', b''))
                if self.max_bytes and len(body) > self.max_bytes:
                    too_large = True
                    break
                if not message.get('more_body'):
                    break
        payload = None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            pass
        invalid_session = False
        if owner and isinstance(payload, dict):
            params = payload.get('params') or {}
            if isinstance(params, dict):
                arguments, meta = params.get('arguments') or {}, params.get('_meta') or {}
                selected = arguments.get('session_id', '') if isinstance(arguments, dict) else ''
                if not selected and isinstance(meta, dict):
                    selected = meta.get('googlevm_session', '')
                if selected:
                    try:
                        await asyncio.to_thread(self.records.check_session, selected, client)
                        sid = selected
                    except (ValueError, TypeError):
                        invalid_session = True
        traffic_id = uuid.uuid4().hex
        common = {'traffic_id':traffic_id, 'http_method':scope.get('method'), 'path':scope.get('path'),
                  'rpc_method':payload.get('method') if isinstance(payload, dict) else None,
                  'rpc_id':payload.get('id') if isinstance(payload, dict) else None}
        await asyncio.to_thread(self.records.traffic_write, sid, traffic_id, 'request', bytes(body), False)
        await asyncio.to_thread(self.records.traffic_write, sid, traffic_id, 'response', b'', False)
        await asyncio.to_thread(self.records.event, sid, 'protocol.request',
            dict(common, direction='request', headers=safe_headers(scope.get('headers', [])),
                 query=scope.get('query_string', b'').decode('latin1'), body_bytes=len(body), complete=not(disconnected or too_large)))
        status, response_offset, complete = None, 0, False

        async def recorded_send(message):
            nonlocal status, response_offset, complete
            if message['type'] == 'http.response.start':
                status = message['status']
                await asyncio.to_thread(self.records.event, sid, 'protocol.response.start',
                    dict(common, direction='response', status=status, elapsed_ms=round((time.monotonic()-started)*1000,2), headers=safe_headers(message.get('headers', []))))
            elif message['type'] == 'http.response.body':
                chunk = message.get('body', b'')
                await asyncio.to_thread(self.records.traffic_write, sid, traffic_id, 'response', chunk, True)
                complete = not message.get('more_body', False)
                await asyncio.to_thread(self.records.event, sid, 'protocol.response',
                    dict(common, direction='response', status=status, offset=response_offset,
                         chunk_bytes=len(chunk), body_bytes=response_offset+len(chunk), complete=complete, elapsed_ms=round((time.monotonic()-started)*1000,2)))
                response_offset += len(chunk)
            elif message['type'] == 'http.response.trailers':
                await asyncio.to_thread(self.records.event, sid, 'protocol.response.trailers',
                    dict(common, direction='response', headers=safe_headers(message.get('headers', []))))
            await send(message)

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type':'http.request','body':bytes(body),'more_body':False}
            return await receive()

        consumed = scope.get('method') != 'POST'
        marker = CURRENT_SESSION.set(sid)
        try:
            if disconnected:
                return
            if too_large or invalid_session:
                await recorded_send({'type':'http.response.start','status':413 if too_large else 403,
                                     'headers':[(b'content-type',b'application/json')]})
                await recorded_send({'type':'http.response.body','body':json.dumps({'error':
                    'Request exceeds configured size limit' if too_large else 'Unknown session for this client'}).encode()})
            else:
                await self.app(scope, replay, recorded_send)
        except BaseException as exc:
            await asyncio.to_thread(self.records.event, sid, 'protocol.error',dict(common, error=type(exc).__name__+': '+str(exc)))
            raise
        finally:
            CURRENT_SESSION.reset(marker)
            if not complete:
                await asyncio.to_thread(self.records.event, sid, 'protocol.incomplete',
                    dict(common,status=status,body_bytes=response_offset,disconnected=disconnected))
