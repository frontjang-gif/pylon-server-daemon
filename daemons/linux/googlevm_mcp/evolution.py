"""Stable MCP gateways; new tools are discovered without tool-list refreshes."""
import asyncio
from typing import Dict, List, Optional

from mcp.types import ToolAnnotations

from .audit import principal, CURRENT_SESSION


def register(mcp, records, data_root, jobs=None):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

    @mcp.tool(name='tool_create', annotations=write, description='Create a new Python run(arguments,ctx) tool with its JSON input_schema. Activates immediately; tests and permissions are optional. No confirmation required.' if records.unrestricted else None)
    async def tool_create(name: str, description: str, code: str, input_schema: Dict,
                          tests: Optional[List[Dict]] = None, permissions: Optional[List[str]] = None,
                          confirm: bool = False, session_id: str = '') -> dict:
        """Create a new named tool proposal. Code defines run(arguments,ctx); tests are [{arguments,expected}]. Permissions: data.read/data.write. Rejects existing names. In unrestricted mode tests are optional and the version activates immediately."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'name': name}
        return await asyncio.to_thread(records.propose, name, description, code, input_schema, permissions or [], tests or [], 0)

    @mcp.tool(name='tool_modify', annotations=write, description='Replace an existing tool with complete code, description and input_schema. Supply its latest base_version; stale edits fail. Activates immediately; tests are optional.' if records.unrestricted else None)
    async def tool_modify(name: str, base_version: int, description: str, code: str, input_schema: Dict,
                          tests: Optional[List[Dict]] = None, permissions: Optional[List[str]] = None,
                          confirm: bool = False, session_id: str = '') -> dict:
        """Propose a replacement version of an existing tool, supplying complete code/schema/tests/permissions. Discover the latest base_version first; stale versions are rejected. In unrestricted mode the new version activates immediately without tests or approval."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'name': name, 'base_version': base_version}
        if base_version < 1:
            raise ValueError('base_version must identify an existing tool version')
        return await asyncio.to_thread(records.propose, name, description, code, input_schema, permissions or [], tests or [], base_version)

    @mcp.tool(name='tool_run', annotations=write, description='Run a named active Python tool with schema-validated arguments. No approval or confirmation required. background=true returns a durable job ID for vm.job.get/cancel.' if records.unrestricted else None)
    async def tool_run(name: str, arguments: Dict, confirm: bool = False, session_id: str = '', background: bool = False) -> dict:
        """Execute an approved active custom tool by name with schema-validated arguments. Discover its schema with vm.tools.discover. Unrestricted mode requires no confirmation or approval; background=true returns a job ID. Saves the call and result in session history."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'name': name}
        if background:
            if not records.unrestricted:
                raise ValueError('Background execution requires unrestricted deployment')
            value = await asyncio.to_thread(records.resolve, name, arguments)
            return await asyncio.to_thread(jobs.submit, {'kind':'tool','version':value,'arguments':arguments,'data_root':str(data_root),'runtime_dir':str(records.directory)}, principal(), CURRENT_SESSION.get())
        return await asyncio.to_thread(records.invoke, name, arguments, data_root)

    @mcp.tool(name='vm.tools.discover', annotations=read)
    async def discover(name: str = '', query: str = '', session_id: str = '') -> dict:
        """Discover proposed/active versioned tools and exact input schemas. Invoke active ones through vm.tools.invoke."""
        return await asyncio.to_thread(records.catalog, name, query)

    @mcp.tool(name='vm.tools.propose', annotations=write, description='Create or version a Python tool; activates immediately. Prefer tool_create/tool_modify for explicit create/update semantics. Tests are optional.' if records.unrestricted else None)
    async def propose(name: str, description: str, code: str, input_schema: Dict,
                      tests: Optional[List[Dict]] = None, permissions: Optional[List[str]] = None,
                      confirm: bool = False, session_id: str = '') -> dict:
        """Propose immutable Python defining run(arguments,ctx), JSON input schema, and tests [{arguments,expected}]. Supported permissions data.read/data.write. Unrestricted mode activates immediately; tests are optional."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'name': name}
        return await asyncio.to_thread(records.propose, name, description, code, input_schema, permissions or [], tests or [])

    @mcp.tool(name='vm.tools.test', annotations=write, description='Optionally run supplied tests against disposable data. Unrestricted VM Python execution; this is not an approval gate or an OS sandbox.' if records.unrestricted else None)
    async def test(name: str, version: int, confirm: bool = False, session_id: str = '') -> dict:
        """Test a proposal in resource-limited subprocesses with disposable data. This does not approve or activate it."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'name': name, 'version': version}
        return await asyncio.to_thread(records.test, name, version, data_root)

    @mcp.tool(name='vm.tools.invoke', annotations=write, description='Compatibility alias for tool_run. Runs the active version; no confirmation required. background=true returns a durable job ID.' if records.unrestricted else None)
    async def invoke(name: str, arguments: Dict, confirm: bool = False, session_id: str = '', background: bool = False) -> dict:
        """Run the currently approved active version with schema validation. Discover first. Unrestricted mode requires no confirmation; background=true returns a durable job ID."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True, 'name': name}
        if background:
            if not records.unrestricted:
                raise ValueError('Background execution requires unrestricted deployment')
            value = await asyncio.to_thread(records.resolve, name, arguments)
            return await asyncio.to_thread(jobs.submit, {'kind':'tool','version':value,'arguments':arguments,'data_root':str(data_root),'runtime_dir':str(records.directory)}, principal(), CURRENT_SESSION.get())
        return await asyncio.to_thread(records.invoke, name, arguments, data_root)

    @mcp.tool(name='vm.session.open', annotations=write, description='Optionally group tool activity under a session_id. Tool traffic is recorded automatically even without this call; chat transcript forwarding is optional.' if records.unrestricted else None)
    async def open_session(title: str = '', confirm: bool = False) -> dict:
        """Start a conversation record. Pass its session_id to subsequent tools and append every user/assistant message. MCP cannot observe unsent chat text."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True}
        sid = await asyncio.to_thread(records.open_session, principal(), title)
        await asyncio.to_thread(records.event, sid, 'session.opened', {'title': title})
        return {'ok': True, 'session_id': sid, 'coverage': 'tools_only',
                'instructions': 'Append each user and assistant turn through vm.session.append with stable message ids. Pass session_id on all subsequent tool calls.'}

    @mcp.tool(name='vm.session.append', annotations=write)
    async def append(session_id: str, messages: List[Dict], confirm: bool = False) -> dict:
        """Save conversation messages [{id?,role,content,timestamp?}]. Stable ids deduplicate retries. Include all turns; only client-supplied messages can be saved."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True}
        return await asyncio.to_thread(records.append, session_id, messages, principal())

    @mcp.tool(name='vm.session.list', annotations=read)
    async def sessions(limit: int = 100, session_id: str = '') -> dict:
        """List this OAuth client's saved conversations and automatic tool-activity streams."""
        return await asyncio.to_thread(records.sessions, principal(), limit)

    @mcp.tool(name='vm.session.get', annotations=read)
    async def session(session_id: str, offset: int = 0, limit: int = 200) -> dict:
        """Read paginated saved messages, tool requests/results/errors and protocol initialization for a session."""
        return await asyncio.to_thread(records.session, session_id, principal(), offset, limit)

    @mcp.tool(name='vm.session.export', annotations=write)
    async def export(session_id: str, confirm: bool = False) -> dict:
        """Export the complete saved session to data/sessions/<id>.json for local browsing/fetching."""
        if not records.unrestricted and confirm is not True:
            return {'ok': True, 'dry_run': True}
        return await asyncio.to_thread(records.export, session_id, data_root, principal())

    @mcp.tool(name='vm.job.get', annotations=read)
    async def job_get(job_id: str, offset: int = 0, length: int = 65536, session_id: str = '') -> dict:
        """Poll a background command/tool job. Completed result is JSON bytes in content_b64; use next_offset for chunks or length=0 for all. Result remains on VM across MCP restarts."""
        return await asyncio.to_thread(jobs.get, job_id, principal(), offset, length)

    @mcp.tool(name='vm.job.cancel', annotations=write)
    async def job_cancel(job_id: str, confirm: bool = False, session_id: str = '') -> dict:
        """Stop this client's background job and its descendants. No confirmation needed in unrestricted mode."""
        if not records.unrestricted and confirm is not True:
            return {'ok':True,'dry_run':True}
        return await asyncio.to_thread(jobs.cancel, job_id, principal())
