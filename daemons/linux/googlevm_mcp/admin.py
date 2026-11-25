"""Local SSH-only management entry; release approval is not an MCP tool."""
import argparse
import json
from pathlib import Path

from .runtime import Runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('operation', choices=['catalog', 'review', 'test', 'activate', 'disable', 'sessions', 'session', 'export', 'traffic', 'youtube-list', 'youtube-capture'])
    args = parser.parse_args()
    import sys
    value = json.load(sys.stdin)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    records = Runtime(config.get('runtime_dir') or args.config.parent / 'state/runtime', config.get('unrestricted', False))
    data_root = Path.home() / 'ai-pylon2/mcp-data'
    op = args.operation
    if op == 'youtube-list':
        from .youtube import list_captures
        result = list_captures(data_root)
    elif op == 'youtube-capture':
        from .youtube import video_id
        from .jobs import Jobs
        identifier = video_id(value['url'])
        sid = records.open_session('local-owner','YouTube capture',automatic=True)
        result = Jobs(records.directory/'jobs').submit({'kind':'youtube','url':value['url'],'backend':value.get('backend','auto'),'max_comments':int(value.get('max_comments',100)),'data_root':str(data_root),'runtime_dir':str(records.directory)}, 'local-owner',sid)
        result.update(video_id=identifier,path='data/youtube/'+identifier+'/capture.json')
    elif op == 'catalog':
        result = records.catalog(query=value.get('query', ''))
    elif op == 'review':
        version = records.version(value['name'], value['version'])
        with records.db() as db:
            version['release_history'] = [dict(row) for row in db.execute(
                'SELECT ts,version,actor,action FROM releases_audit WHERE name=? ORDER BY id DESC LIMIT 100', (value['name'],)).fetchall()]
        result = {'ok': True, 'version': version}
    elif op == 'test':
        result = records.test(value['name'], value['version'], data_root)
    elif op == 'activate':
        result = records.activate(value['name'], value['version'], value['hash'])
    elif op == 'disable':
        result = records.disable(value['name'])
    elif op == 'sessions':
        result = records.sessions(limit=value.get('limit', 100))
    elif op == 'traffic':
        result = records.traffic_read(value['session_id'], value['traffic_id'], value['direction'], value.get('offset',0), value.get('length',65536))
    elif op == 'session':
        result = records.session(value['session_id'], offset=value.get('offset', 0), limit=value.get('limit', 200))
    else:
        result = records.export(value['session_id'], data_root)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
