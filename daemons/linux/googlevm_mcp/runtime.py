"""Durable tool versions, release approvals, sessions and transcript events."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from jsonschema import Draft202012Validator


def stamp():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value):
        raise ValueError('Invalid identifier')
    return value


def redact(value):
    if isinstance(value, dict):
        return {key: '[redacted]' if re.search(r'authorization|password|secret|cookie|access_token|refresh_token|api_key', str(key), re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if hasattr(value, 'model_dump'):
        return redact(value.model_dump(mode='json'))
    return value


class Runtime:
    def __init__(self, directory, unrestricted=False):
        self.unrestricted = unrestricted
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'records.sqlite3'
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, principal TEXT NOT NULL, title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, coverage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, external_id TEXT, UNIQUE(session,external_id));
CREATE INDEX IF NOT EXISTS session_events ON events(session,id);
CREATE TABLE IF NOT EXISTS versions(name TEXT NOT NULL, version INTEGER NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL, tested INTEGER NOT NULL DEFAULT 0, approved INTEGER NOT NULL DEFAULT 0, test_result TEXT, PRIMARY KEY(name,version));
CREATE TABLE IF NOT EXISTS releases(name TEXT PRIMARY KEY, version INTEGER);
CREATE TABLE IF NOT EXISTS releases_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, name TEXT NOT NULL, version INTEGER, actor TEXT NOT NULL, action TEXT NOT NULL);
''')

    @contextmanager
    def db(self):
        # Every connection belongs to one worker. Writers serialize inside it,
        # never across an await. WAL keeps exports/readers safe during writes.
        with self.lock:
            db = sqlite3.connect(self.path, timeout=20)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA busy_timeout=20000')
            try:
                with db:
                    yield db
            finally:
                db.close()

    def open_session(self, principal, title='', automatic=False):
        sid = ('stream_' + hashlib.sha256((principal + stamp()[:10]).encode()).hexdigest()[:32]) if automatic else uuid.uuid4().hex
        now = stamp()
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?,?)',
                       (sid, principal, title[:300] or ('Unassigned client tool activity' if automatic else 'Conversation'), now, now, 'tools_only'))
        return sid

    def check_session(self, sid, principal=None):
        identifier(sid)
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        if not row or principal is not None and row['principal'] != principal:
            raise ValueError('Unknown session for this client')
        return dict(row)

    def event(self, sid, kind, payload, external_id=None):
        now = stamp()
        serialized = json.dumps(redact(payload), ensure_ascii=False)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO events(session,ts,kind,payload,external_id) VALUES(?,?,?,?,?)',
                       (sid, now, kind, serialized, external_id))
            db.execute('UPDATE sessions SET updated=? WHERE id=?', (now, sid))

    def traffic_path(self, sid, traffic_id, direction):
        identifier(sid)
        if not re.fullmatch(r'[0-9a-f]{32}', traffic_id) or direction not in ('request', 'response'):
            raise ValueError('Invalid traffic reference')
        return self.directory / 'traffic' / sid / (traffic_id + '.' + direction + '.body')

    def traffic_write(self, sid, traffic_id, direction, content, append=True):
        target = self.traffic_path(sid, traffic_id, direction)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open('ab' if append else 'wb') as stream:
            stream.write(content)

    def traffic_read(self, sid, traffic_id, direction, offset=0, length=65536, principal=None):
        import base64
        self.check_session(sid, principal)
        offset, length = int(offset), int(length)
        if offset < 0 or length < 0:
            raise ValueError('offset and length must be nonnegative')
        target = self.traffic_path(sid, traffic_id, direction)
        with target.open('rb') as stream:
            stream.seek(offset)
            content = stream.read(length or -1)
        total = target.stat().st_size
        return {'ok':True,'session_id':sid,'traffic_id':traffic_id,'direction':direction,
                'offset':offset,'next_offset':offset+len(content),'total_bytes':total,
                'content_b64':base64.b64encode(content).decode('ascii'),
                'text':content.decode('utf-8','replace'),'has_more':offset+len(content)<total}

    def append(self, sid, messages, principal):
        self.check_session(sid, principal)
        if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
            raise ValueError('Provide 1 to 100 messages')
        for message in messages:
            if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'system', 'tool') or not isinstance(message.get('content'), (str, list, dict)):
                raise ValueError('Messages need role and content')
            if len(json.dumps(message)) > 2 * 1024 * 1024:
                raise ValueError('Message exceeds 2 MiB; split it into parts')
            if 'id' in message:
                identifier(message['id'])
        for message in messages:
            self.event(sid, 'message', message, message.get('id'))
        with self.db() as db:
            db.execute("UPDATE sessions SET coverage='client_transcript' WHERE id=?", (sid,))
        return {'ok': True, 'session_id': sid, 'coverage': 'client_transcript'}

    def sessions(self, principal=None, limit=100):
        limit = min(500, max(1, int(limit)))
        with self.db() as db:
            rows = db.execute('SELECT s.*, (SELECT count(*) FROM events WHERE session=s.id) AS event_count FROM sessions s ' +
                              ('WHERE principal=? ' if principal is not None else '') + 'ORDER BY updated DESC LIMIT ?',
                              (principal, limit) if principal is not None else (limit,)).fetchall()
        return {'ok': True, 'items': [dict(row) for row in rows]}

    def session(self, sid, principal=None, offset=0, limit=200):
        result = self.check_session(sid, principal)
        offset, limit = max(0, int(offset)), min(1000, max(1, int(limit)))
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM events WHERE session=?', (sid,)).fetchone()[0]
            rows = db.execute('SELECT * FROM events WHERE session=? ORDER BY id LIMIT ? OFFSET ?', (sid, limit, offset)).fetchall()
            protocol = db.execute("SELECT kind,ts,payload FROM events WHERE session=? AND kind LIKE 'protocol.%' ORDER BY id", (sid,)).fetchall()
            calls = db.execute("SELECT count(*) FROM events WHERE session=? AND kind='tool.request'", (sid,)).fetchone()[0]
        from .diagnostics import summarize
        diagnostics = summarize([{**dict(row),'payload':json.loads(row['payload'])} for row in protocol], calls)
        return {'ok': True, 'session': result, 'diagnostics': diagnostics, 'events': [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows],
                'offset': offset, 'total': total, 'has_more': offset + len(rows) < total}

    def export(self, sid, data_root, principal=None):
        self.check_session(sid, principal)
        with self.db() as db:
            rows = db.execute('SELECT * FROM events WHERE session=? ORDER BY id', (sid,)).fetchall()
        folder = Path(data_root) / 'sessions'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (sid + '.json')
        temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            traffic_ids = sorted({json.loads(row['payload'])['traffic_id'] for row in rows if row['kind'].startswith('protocol.') and 'traffic_id' in json.loads(row['payload'])})
            traffic = [self.traffic_read(sid, tid, direction, length=0) for tid in traffic_ids for direction in ('request','response')]
            temporary.write_text(json.dumps({'traffic':traffic, 'session': self.check_session(sid), 'events':
                [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]}, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {'ok': True, 'path': 'data/sessions/' + sid + '.json', 'events': len(rows)}

    def propose(self, name, description, code, schema, permissions, tests, expected_version=None):
        from .runner import validate_code
        identifier(name)
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', name):
            raise ValueError('Use a lowercase tool name with underscores')
        if not description or len(description) > 4000 or (not self.unrestricted and len(code.encode()) > 100000):
            raise ValueError('Provide a description and code under 100 KB')
        if not isinstance(permissions, list) or set(permissions) - {'data.read', 'data.write'}:
            raise ValueError('Supported permissions: data.read, data.write. Network/commands use existing VM tools.')
        Draft202012Validator.check_schema(schema)
        def local_references(value):
            if isinstance(value, dict):
                if '$ref' in value and not str(value['$ref']).startswith('#'):
                    raise ValueError('Only local JSON schema references are supported')
                for item in value.values():
                    local_references(item)
            elif isinstance(value, list):
                for item in value:
                    local_references(item)
        local_references(schema)
        if schema.get('type') != 'object':
            raise ValueError('Input schema must describe an object')
        if not isinstance(tests, list) or not (0 if self.unrestricted else 1) <= len(tests) <= 10 or any(not isinstance(t, dict) or 'arguments' not in t or 'expected' not in t for t in tests):
            raise ValueError('Provide 1 to 10 tests with arguments and expected JSON result')
        validate_code(code, self.unrestricted)
        for test in tests:
            Draft202012Validator(schema).validate(test['arguments'])
        payload = {'description': description, 'code': code, 'input_schema': schema,
                   'permissions': sorted(set(permissions)), 'tests': tests}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db() as db:
            latest = db.execute('SELECT coalesce(max(version),0) FROM versions WHERE name=?', (name,)).fetchone()[0]
            if expected_version is not None and latest != expected_version:
                raise ValueError('Tool already exists or base_version is stale; discover its latest version first')
            same = db.execute('SELECT version FROM versions WHERE name=? AND hash=?', (name, digest)).fetchone()
            if same:
                return {'ok': True, 'name': name, 'version': same['version'], 'hash': digest, 'unchanged': True}
            version = db.execute('SELECT coalesce(max(version),0)+1 FROM versions WHERE name=?', (name,)).fetchone()[0]
            db.execute('INSERT INTO versions(name,version,hash,payload,created) VALUES(?,?,?,?,?)', (name, version, digest, encoded, stamp()))
            if self.unrestricted:
                self._release(db, name, version, 'owner-config', 'auto-activate')
        return {'ok': True, 'name': name, 'version': version, 'hash': digest, 'status': 'active' if self.unrestricted else 'proposed'}

    def version(self, name, version):
        identifier(name)
        with self.db() as db:
            row = db.execute('SELECT * FROM versions WHERE name=? AND version=?', (name, int(version))).fetchone()
        if not row:
            raise ValueError('Unknown tool version')
        value = dict(row)
        value['unrestricted'] = self.unrestricted
        value.update(json.loads(value.pop('payload')))
        value['test_result'] = json.loads(value['test_result']) if value['test_result'] else None
        return value

    def catalog(self, name='', query='', include_code=False):
        with self.db() as db:
            rows = db.execute('SELECT name,version,hash,created,tested,approved FROM versions ' +
                              ('WHERE name=? ' if name else '') + 'ORDER BY name,version DESC LIMIT 500', (name,) if name else ()).fetchall()
            releases = dict(db.execute('SELECT name,version FROM releases').fetchall())
        items = []
        for row in rows:
            value = self.version(row['name'], row['version'])
            value['active'] = releases.get(value['name']) == value['version']
            if query.casefold() not in (value['name'] + ' ' + value['description']).casefold():
                continue
            if not include_code:
                value.pop('code')
                value.pop('tests')
                if value['test_result']:
                    value['test_result'] = {'passed': value['test_result']['passed'], 'case_count': len(value['test_result']['cases'])}
            items.append(value)
        return {'ok': True, 'items': items, 'gateway': 'vm.tools.invoke', 'activation': 'Automatic on create/modify' if self.unrestricted else 'Owner review in local Google VM MCP app',
                'unrestricted': self.unrestricted, 'isolation': 'VM user Python process; no execution limits' if self.unrestricted else 'Resource-limited Python subprocess with restricted imports; not an OS security sandbox'}

    def test(self, name, version, data_root):
        from .runner import execute
        value = self.version(name, version)
        results = []
        # Tests use a disposable data folder, never production files.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='pylon-tool-test-') as directory:
            for case in value['tests']:
                result = execute(value, case['arguments'], directory, self.unrestricted)
                results.append({'ok': result.get('ok', False) and result.get('result') == case['expected'],
                                'expected': case['expected'], 'actual': result})
        passed = all(item['ok'] for item in results)
        with self.db() as db:
            db.execute('UPDATE versions SET tested=?,test_result=? WHERE name=? AND version=?',
                       (int(passed), json.dumps({'passed': passed, 'cases': results}), name, int(version)))
        return {'ok': True, 'passed': passed, 'name': name, 'version': version, 'cases': results}

    def _release(self, db, name, version, actor, action):
        db.execute('UPDATE versions SET approved=1 WHERE name=? AND version=?', (name, version))
        db.execute('INSERT INTO releases VALUES(?,?) ON CONFLICT(name) DO UPDATE SET version=excluded.version', (name, version))
        db.execute('INSERT INTO releases_audit(ts,name,version,actor,action) VALUES(?,?,?,?,?)', (stamp(), name, version, actor, action))

    def activate(self, name, version, digest, actor='owner'):
        value = self.version(name, version)
        if value['hash'] != digest or (not self.unrestricted and not value['tested']):
            raise ValueError('Review the exact hash and pass all tests before activation')
        with self.db() as db:
            db.execute('UPDATE versions SET approved=1 WHERE name=? AND version=?', (name, version))
            db.execute('INSERT INTO releases VALUES(?,?) ON CONFLICT(name) DO UPDATE SET version=excluded.version', (name, version))
            db.execute('INSERT INTO releases_audit(ts,name,version,actor,action) VALUES(?,?,?,?,?)', (stamp(), name, version, actor, 'activate'))
        return {'ok': True, 'name': name, 'active_version': version}

    def disable(self, name, actor='owner'):
        identifier(name)
        with self.db() as db:
            db.execute('DELETE FROM releases WHERE name=?', (name,))
            db.execute('INSERT INTO releases_audit(ts,name,actor,action) VALUES(?,?,?,?)', (stamp(), name, actor, 'disable'))
        return {'ok': True, 'name': name, 'disabled': True}

    def resolve(self, name, arguments):
        with self.db() as db:
            row = db.execute('SELECT version FROM releases WHERE name=?', (name,)).fetchone()
        if not row:
            raise ValueError('Tool is not active; ask the owner to review it in Google VM MCP')
        value = self.version(name, row['version'])
        if not value['approved']:
            raise ValueError('Tool version is not approved')
        Draft202012Validator(value['input_schema']).validate(arguments)
        return value

    def invoke(self, name, arguments, data_root):
        from .runner import execute
        value = self.resolve(name, arguments)
        return dict(execute(value, arguments, data_root, self.unrestricted), name=name, version=value['version'], hash=value['hash'])
