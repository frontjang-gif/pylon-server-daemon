"""Resource-limited worker for reviewed tools, not a general Python sandbox."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SAFE_MODULES = {'math', 'json', 're', 'statistics', 'datetime', 'decimal', 'fractions', 'collections', 'itertools', 'functools', 'csv', 'hashlib', 'string'}
FORBIDDEN = {'open', 'exec', 'eval', 'compile', '__import__', 'getattr', 'setattr', 'delattr', 'globals', 'locals', 'vars', 'breakpoint', 'input', 'help', 'type', 'object', 'super'}


def validate_code(code, unrestricted=False):
    tree = ast.parse(code)
    if not any(isinstance(node, ast.FunctionDef) and node.name == 'run' for node in tree.body):
        raise ValueError('Define run(arguments, ctx) returning a JSON value')
    if unrestricted:
        return tree
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.split('.')[0] not in SAFE_MODULES for alias in node.names):
            raise ValueError('Import is outside the managed-tool allowlist')
        if isinstance(node, ast.ImportFrom) and (node.level or (node.module or '').split('.')[0] not in SAFE_MODULES):
            raise ValueError('Import is outside the managed-tool allowlist')
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN or node.id.startswith('__')):
            raise ValueError('Disallowed Python operation: ' + node.id)
        if isinstance(node, ast.Attribute) and node.attr.startswith('_'):
            raise ValueError('Private Python attributes are not allowed')
    return tree


def execute(version, arguments, data_root, unrestricted=False):
    validate_code(version['code'], unrestricted)
    with tempfile.TemporaryDirectory(prefix='pylon-tool-') as temporary:
        path = Path(temporary)
        (path / 'code.py').write_text(version['code'], encoding='utf-8')
        (path / 'input.json').write_text(json.dumps({'arguments': arguments, 'permissions': version['permissions'],
                                                   'unrestricted': unrestricted, 'data_root': str(Path(data_root).resolve())}), encoding='utf-8')
        try:
            result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--worker', str(path)],
                cwd=path, input='', text=True, encoding='utf-8', capture_output=True, timeout=None if unrestricted else 12,
                env=dict(os.environ) if unrestricted else {'PATH': os.environ.get('PATH', ''), 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                     'HOME': str(path), 'PYTHONIOENCODING': 'utf-8'}, start_new_session=os.name != 'nt')
        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': 'Tool exceeded its 12-second wall limit'}
        output = path / 'result.json'
        if result.returncode != 0 or not output.exists():
            return {'ok': False, 'error': 'Worker failed or exceeded resource limits', 'returncode': result.returncode,
                    'stderr': result.stderr[-4000:]}
        if not unrestricted and output.stat().st_size > 1024 * 1024:
            return {'ok': False, 'error': 'Tool result exceeds 1 MiB'}
        return json.loads(output.read_text(encoding='utf-8'))


class DataContext:
    def __init__(self, root, permissions, unrestricted=False):
        self._unrestricted = unrestricted
        self._root = Path(root).resolve()
        self._permissions = permissions

    def _path(self, path, permission):
        if not self._unrestricted and permission not in self._permissions:
            raise PermissionError('Missing permission: ' + permission)
        if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts or '\\' in path:
            raise ValueError('Use a relative data path')
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root) or '.pylon' in target.relative_to(self._root).parts:
            raise ValueError('Path escapes tool data or accesses internal records')
        return target

    def read_text(self, path):
        target = self._path(path, 'data.read')
        if not self._unrestricted and target.stat().st_size > 1024 * 1024:
            raise ValueError('Read limit is 1 MiB')
        return target.read_text(encoding='utf-8')

    def write_text(self, path, content):
        target = self._path(path, 'data.write')
        if not isinstance(content, str) or (not self._unrestricted and len(content.encode()) > 1024 * 1024):
            raise ValueError('Write limit is 1 MiB')
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.tool-', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(content)
            Path(temporary).replace(target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {'path': path, 'bytes': len(content.encode())}


def worker(directory):
    path = Path(directory)
    value = json.loads((path / 'input.json').read_text(encoding='utf-8'))
    unrestricted = value.get('unrestricted', False)
    if not unrestricted and os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 ** 2, 256 * 1024 ** 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 ** 2, 2 * 1024 ** 2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    path = Path(directory)
    try:
        value = json.loads((path / 'input.json').read_text(encoding='utf-8'))
        code = (path / 'code.py').read_text(encoding='utf-8')
        validate_code(code, unrestricted)
        namespace = {}
        # Output goes to capped files instead of unbounded parent-process pipes.
        with (path / 'stdout.log').open('w', encoding='utf-8') as output:
            from contextlib import redirect_stdout, redirect_stderr
            with redirect_stdout(output), redirect_stderr(output):
                exec(compile(code, '<reviewed-tool>', 'exec'), namespace)
                result = namespace['run'](value['arguments'], DataContext(value['data_root'], value['permissions'], unrestricted))
        response = {'ok': True, 'result': result, 'stdout': (path / 'stdout.log').read_text(encoding='utf-8') if unrestricted else (path / 'stdout.log').read_text(encoding='utf-8')[:16000]}
    except BaseException as exc:
        response = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}
    (path / 'result.json').write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--worker':
    worker(sys.argv[2])
