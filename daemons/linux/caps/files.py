"""Bounded home-directory file access; never follow links outside the root."""
import base64
import os
import tempfile
from pathlib import Path

CAPABILITY = 'cap:linux.files'
LIMIT = 1024 * 1024


def path(value, ctx=None):
    root = Path(os.environ.get('PYLON2_FILE_ROOT', str(Path.home()))).resolve()
    candidate = (root / str(value or '.')).resolve()
    if not (ctx or {}).get('unrestricted') and candidate != root and root not in candidate.parents:
        raise ValueError('Path is outside the configured Linux file root')
    return candidate


def listing(args, ctx):
    root = path(args.get('path'), ctx)
    entries = []
    with os.scandir(root) as scan:
        for item in scan:
            if len(entries) >= 2000:
                return {'ok': True, 'entries': entries, 'truncated': True}
            stat = item.stat(follow_symlinks=False)
            entries.append({'name': item.name, 'type': 'link' if item.is_symlink() else 'dir' if item.is_dir() else 'file',
                            'size': stat.st_size, 'mtime': stat.st_mtime})
    return {'ok': True, 'entries': entries, 'truncated': False}


def read(args, ctx):
    with path(args.get('path'), ctx).open('rb') as stream:
        offset = int(args.get('offset', 0))
        length = args.get('length')
        if offset < 0 or (length is not None and int(length) < 0):
            raise ValueError('offset and length must be nonnegative')
        stream.seek(offset)
        content = stream.read(-1 if length is None else int(length)) if ctx.get('unrestricted') else stream.read(LIMIT + 1)
    if not ctx.get('unrestricted') and len(content) > LIMIT:
        raise ValueError('File exceeds the 1 MiB response limit')
    return {'ok': True, 'content_b64': base64.b64encode(content).decode('ascii'), 'size': len(content)}


def write(args, ctx):
    target = path(args.get('path'), ctx)
    content = args.get('content', '').encode('utf-8') if 'content_b64' not in args else base64.b64decode(args['content_b64'], validate=True)
    if not ctx.get('unrestricted') and len(content) > LIMIT:
        raise ValueError('File exceeds the 1 MiB write limit')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pylon-write-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'ok': True, 'size': len(content)}


METHODS = {'list': listing, 'read': read, 'write': write}
