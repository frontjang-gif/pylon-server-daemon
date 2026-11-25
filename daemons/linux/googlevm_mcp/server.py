"""Official MCP SDK transport over the VM's existing Linux capabilities."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from typing import Dict, Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.routing import Route

from .oauth import OAuthProvider
from .oauth_routes import OAuthRoutes
from .audit import RecordedMCP, SessionMiddleware, SDK2
from .runtime import Runtime

VERSION = '1.7.0'
SERVICE = 'ai-pylon2-googlevm-mcp'


class OwnerVerifier(TokenVerifier):
    def __init__(self, provider):
        self.provider = provider

    async def verify_token(self, token):
        principal = await asyncio.to_thread(self.provider.authenticate, token)
        if not principal:
            return None
        return AccessToken(token=token, client_id=principal.removeprefix('oauth:'),
                           scopes=[self.provider.scope], resource=self.provider.resource)


def create_app(config, root):
    root = Path(root).resolve()
    provider = OAuthProvider(config['oauth'], root)
    try:
        return _build_app(config, root, provider)
    except BaseException:
        provider.close()
        raise


def _build_app(config, root, provider):
    sys.path.insert(0, str(Path(config['capabilities_dir']).resolve()))
    # These are the SAME implementations used by the Linux daemon. There is no
    # second file store, command runner, or downloader in this MCP service.
    unrestricted = config.get('unrestricted', False)
    files = importlib.import_module('googlevm_mcp.host_caps.files' if unrestricted else 'caps.files')
    terminal = importlib.import_module('googlevm_mcp.host_caps.terminal' if unrestricted else 'caps.terminal')
    download = importlib.import_module('caps.download')
    data_root = Path.home() / 'ai-pylon2/mcp-data'

    def data_path(path):
        if not isinstance(path, str) or '\\' in path or '\x00' in path or Path(path).is_absolute() or '..' in Path(path).parts or '.pylon' in Path(path).parts:
            raise ValueError('Use a relative path within MCP data')
        parent = data_root.resolve()
        target = (parent / path).resolve()
        if not target.is_relative_to(parent):
            raise ValueError('Path escapes MCP data')
        return str(target)
    hostname = urlsplit(provider.issuer).netloc
    unrestricted = config.get('unrestricted', False)
    cap_ctx = {'unrestricted': unrestricted}
    records = Runtime(config.get('runtime_dir') or root / 'state/runtime', unrestricted)
    from .jobs import Jobs
    jobs = Jobs(records.directory / 'jobs')
    instructions = (
        'Execute on the Google VM as its Linux user. Save durable results under ~/ai-pylon2/mcp-data '
        'using vm.data.write; local ai-pylon2 can browse/fetch them into a workspace. '
        'Downloads use ~/Downloads/ai-pylon2. Windows drives require a separate connection. '
        'Tool calls and full MCP traffic are recorded automatically. Optionally use vm.session.open '
        'and pass session_id to group calls; chat text is saved only if explicitly supplied. '
        'Use tool_create/tool_modify for versioned Python run(arguments,ctx) functions and tool_run to execute. '
        'Discover schemas with vm.tools.discover. When a user shares a YouTube video URL, call youtube_capture '
        'to save its information on this VM unless asked not to fetch. Poll vm.job.get and report partial or blocked components honestly. '
        + ('Unrestricted mode: no confirmation, tests or owner approval required; new versions activate immediately. '
           'Use background=true for long commands/tools and vm.job.get/cancel to track them. '
           'Command timeout=0 disables its deadline; file reads accept offset/length for chunks.'
           if unrestricted else 'Reviewed mode: writes require confirm=true; test proposals and request owner activation.')
    )
    transport = dict(json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=[hostname, '127.0.0.1:*', 'localhost:*'],
                                                     allowed_origins=[provider.issuer]))
    mcp = RecordedMCP('Google VM', records=records, instructions=instructions,
        token_verifier=OwnerVerifier(provider),
        auth=AuthSettings(issuer_url=AnyHttpUrl(provider.issuer),
                          resource_server_url=AnyHttpUrl(provider.resource), required_scopes=[provider.scope],
                          **({'validate_token_resource':True} if SDK2 else {})),
        **({'version':VERSION} if SDK2 else transport))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

    @mcp.tool(name='vm.status', annotations=read)
    async def vm_status(session_id: str = '') -> dict:
        """Read VM home, downloader availability, and MCP version."""
        return {'ok': True, 'service': SERVICE, 'version': VERSION, 'home': str(Path.home()),
                'download_root': str(Path.home() / 'Downloads/ai-pylon2'), 'data_root': str(data_root)}

    @mcp.tool(name='vm.data.list', annotations=read)
    async def list_data(path: str = '', session_id: str = '') -> dict:
        """List durable MCP results, relative to ~/ai-pylon2/mcp-data."""
        target = await asyncio.to_thread(data_path, path)
        result = await asyncio.to_thread(files.listing, {'path': target}, cap_ctx)
        if result.get('ok'):
            result['entries'] = [entry for entry in result['entries'] if entry['name'] != '.pylon']
        return result

    @mcp.tool(name='vm.data.read', annotations=read)
    async def read_data(path: str, offset: int = 0, length: Optional[int] = None, session_id: str = '') -> dict:
        """Read durable data as base64; optional offset/length support chunks. No size cap in unrestricted mode."""
        target = await asyncio.to_thread(data_path, path)
        return await asyncio.to_thread(files.read, {'path': target, 'offset':offset, 'length':length}, cap_ctx)

    @mcp.tool(name='vm.data.write', annotations=write)
    async def write_data(path: str, content: str, confirm: bool = False, session_id: str = '') -> dict:
        """Persist UTF-8 results for local ai-pylon2 to fetch. Unrestricted mode needs no confirmation or size cap."""
        target = await asyncio.to_thread(data_path, path)
        if not unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'path': target}
        return await asyncio.to_thread(files.write, {'path': target, 'content': content}, cap_ctx)

    @mcp.tool(name='vm.files.list', annotations=read)
    async def list_files(path: str = '.', session_id: str = '') -> dict:
        """List a directory within the VM home; at most 2000 entries."""
        return await asyncio.to_thread(files.listing, {'path': path}, cap_ctx)

    @mcp.tool(name='vm.files.read', annotations=read)
    async def read_file(path: str, offset: int = 0, length: Optional[int] = None, session_id: str = '') -> dict:
        """Read a VM file as content_b64. Unrestricted deployment has no size cap; optional offset/length support chunks."""
        return await asyncio.to_thread(files.read, {'path': path, 'offset':offset, 'length':length}, cap_ctx)

    @mcp.tool(name='vm.files.write', annotations=write)
    async def write_file(path: str, content: str, confirm: bool = False, session_id: str = '') -> dict:
        """Write UTF-8 text on the VM. Unrestricted mode needs no confirmation or size cap."""
        if not unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'path': path}
        return await asyncio.to_thread(files.write, {'path': path, 'content': content}, cap_ctx)

    @mcp.tool(name='vm.command', annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def command(cmd: str, cwd: str = '', timeout: float = 0, confirm: bool = False, session_id: str = '', background: bool = False) -> dict:
        """Run as the VM user. Unrestricted mode needs no confirmation, timeout=0 disables deadline, no output cap. Set background=true to return a durable job ID and poll vm.job.get."""
        if not unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'cmd': cmd, 'cwd': cwd}
        options = {'cmd':cmd,'cwd':cwd,'timeout':timeout}
        if background:
            if not unrestricted:
                raise ValueError('Background execution requires unrestricted deployment')
            from .audit import CURRENT_SESSION, principal
            return await asyncio.to_thread(jobs.submit, {'kind':'command','arguments':options,'capabilities_dir':config['capabilities_dir'],'runtime_dir':str(records.directory)}, principal(), CURRENT_SESSION.get())
        return await asyncio.to_thread(terminal.run, options, cap_ctx)

    @mcp.tool(name='vm.download.submit', annotations=write)
    async def submit_download(url: str, method: str = 'GET', headers: Optional[Dict[str, str]] = None,
                              body: str = '', filename: str = '', confirm: bool = False, session_id: str = '') -> dict:
        """Enqueue a URL download with optional custom headers/body. Returns job id. No confirmation needed in unrestricted mode."""
        if not unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'url': url, 'method': method}
        options = {'url': url, 'method': method, 'headers': headers or {}}
        if body:
            options['body'] = body
        if filename:
            options['filename'] = filename
        return await asyncio.to_thread(download.submit, options, {})

    @mcp.tool(name='vm.download.status', annotations=read)
    async def download_status(job_id: str, session_id: str = '') -> dict:
        """Read progress, saved path, and errors for a VM downloader job."""
        return await asyncio.to_thread(download.status, {'job_id': job_id}, {})

    @mcp.tool(name='youtube_capture', annotations=write)
    async def capture_youtube(url: str, backend: str = 'auto', max_comments: int = 100,
                              confirm: bool = False, session_id: str = '') -> dict:
        """Fetch information for a YouTube URL shared in the prompt: title, description, channel, thumbnail, captions and comments where available. Saves durable data/youtube/<video_id>/ on this VM, fetchable by personal/web-youtube locally through VIS. Returns a background job ID; poll vm.job.get. backend=auto/http/browser; max_comments=0 means all for HTTP capture. Missing or blocked components are reported, never fabricated."""
        from .youtube import video_id
        from .audit import CURRENT_SESSION, principal
        identifier = video_id(url)
        if not unrestricted and confirm is not True:
            return {'ok':True,'dry_run':True,'video_id':identifier}
        if backend not in ('auto','http','browser') or max_comments < 0:
            raise ValueError('backend must be auto/http/browser and max_comments nonnegative')
        job = await asyncio.to_thread(jobs.submit, {'kind':'youtube','url':url,'backend':backend,
            'max_comments':max_comments,'data_root':str(data_root),'runtime_dir':str(records.directory)}, principal(), CURRENT_SESSION.get())
        return dict(job,video_id=identifier,path='data/youtube/'+identifier+'/capture.json',local_app='personal/web-youtube')

    from .evolution import register
    register(mcp, records, data_root, jobs)
    app = mcp.streamable_http_app(**dict(transport, max_request_body_size=sys.maxsize if unrestricted else 2*1024*1024)) if SDK2 else mcp.streamable_http_app()
    app.add_middleware(SessionMiddleware, records=records, provider=provider, max_bytes=0 if unrestricted else 2*1024*1024)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application):
                yield
        finally:
            await asyncio.to_thread(provider.close)

    async def health(request):
        tools = await mcp.list_tools()
        return JSONResponse({'ok': True, 'service': SERVICE, 'version': VERSION,
                             'auth_mode': 'oauth', 'unrestricted':unrestricted, 'tool_count': len(tools),
                             'tools': [{'name': tool.name, 'description': tool.description} for tool in tools]})

    # OAuth registration/owner approval is separate from the SDK's authenticated
    # resource transport. Do not duplicate the SDK's protected-resource routes.
    app.router.routes.extend(route for route in OAuthRoutes(provider).routes()
                             if 'oauth-protected-resource' not in route.path)
    app.router.routes.append(Route('/health', health))
    app.router.lifespan_context = lifespan
    app.state.oauth = provider
    app.state.records = records
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=7678)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    import uvicorn
    uvicorn.run(create_app(config, args.config.parent), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
