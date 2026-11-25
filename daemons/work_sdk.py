"""Small SDK for portable endpoint programs: VFS data locality and job progress.

VFS mappings are configured by the endpoint operator, never supplied by remote
jobs. Mapped paths use the endpoint's existing filesystem/provider session.
This is not a sandbox and a local write is not a verified cloud commit.
"""
import json,math,os,re,shutil,tempfile,time
from pathlib import Path

CONFIG=Path.home()/'.pylon2'/'vfs-mounts.json'
MAX_PROGRESS=4096
PROXY_LIMIT=128*1024
PROXY_TOTAL=384*1024
STREAM_FILE=16*1024*1024
STREAM_TOTAL=32*1024*1024
STREAM_CHUNK=128*1024
PREFIX=re.compile(r'^vfs://(?:gdrive|alias|mnt)://[^?#\\]+$')

def _segments(s):
    parts=s.split('/')
    if any(not p or p in ('.','..') or any(ord(c)<32 or c in '\\:%?#' for c in p) or p.rstrip(' .')!=p or p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for p in parts):raise ValueError('Invalid VFS path component')
    return parts

def mounts():
    if not CONFIG.is_file():return []
    if CONFIG.stat().st_size>65536:raise ValueError('VFS mapping configuration too large')
    rows=json.loads(CONFIG.read_text(encoding='utf-8'))
    if not isinstance(rows,list) or len(rows)>64:raise ValueError('Invalid VFS mapping configuration')
    result=[];seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'prefix','root','mode'}:raise ValueError('Invalid VFS mapping')
        prefix=row['prefix'];root=row['root'];mode=row['mode']
        if not isinstance(prefix,str) or not PREFIX.fullmatch(prefix) or prefix in seen:raise ValueError('Invalid/duplicate VFS prefix')
        _segments(prefix.split('://',2)[2])
        if not isinstance(root,str) or not Path(root).is_absolute() or mode not in ('read','read_write'):raise ValueError('Invalid VFS root or mode')
        seen.add(prefix);result.append({'prefix':prefix,'root':Path(root),'mode':mode})
    return sorted(result,key=lambda r:-len(r['prefix']))

def _link(p):
    return p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction())

def validate_uri(url):
    if not isinstance(url,str) or len(url)>4096 or not PREFIX.fullmatch(url):raise ValueError('Unsupported VFS URI')
    _segments(url.split('://',2)[2])
    return url

def requirements(value):
    if not isinstance(value,list) or len(value)>32:raise ValueError('Up to 32 VFS requirements supported')
    result=[];seen=set()
    for item in value:
        if not isinstance(item,dict) or set(item)-{'url','mode','route'} or item.get('mode') not in ('read','write'):raise ValueError('VFS requirement needs URL and read/write mode')
        url=validate_uri(item.get('url'));route=item.get('route','direct')
        if url in seen or route not in ('direct','proxy','proxy_stream'):raise ValueError('Duplicate URL or invalid VFS access route')
        if route in ('proxy','proxy_stream') and not url.startswith('vfs://gdrive://'):raise ValueError('Proxy v1 supports explicit Google Drive paths only')
        seen.add(url);result.append({'url':url,'mode':item['mode'],'route':route})
    if len(json.dumps(result,ensure_ascii=True))>32768:raise ValueError("VFS declaration too large")
    return result

def check_requirements(value):
    for item in requirements(value):
        if item['route'] in ('proxy','proxy_stream'):continue
        write=item['mode']=='write';p=resolve(item['url'],write)
        if write and (p.exists() or not p.parent.is_dir()):raise ValueError('VFS output must be new and parent must exist')
        if not write and not p.is_file():raise ValueError('VFS input unavailable')
def resolve(url,write=False):
    validate_uri(url)
    manifest=os.environ.get('PYLON_WORK_VFS_MANIFEST')
    if manifest:
        f=Path(manifest)
        if f.stat().st_size>65536:raise ValueError('Invalid VFS job manifest')
        entries=json.loads(f.read_text(encoding='utf-8'))
        for i,row in enumerate(entries):
            if row['url']!=url:continue
            if write and row['mode']!='write':raise PermissionError('Declared input is read-only')
            if row['route'] in ('proxy','proxy_stream'):
                if write and row['mode']!='write':raise PermissionError('Read-only proxy input')
                p=f.parent/'proxy'/str(i)
                if _link(p) or _link(p.parent):raise ValueError('Linked proxy path')
                return p
            break
        else:raise PermissionError('VFS URI not declared for this job')
    for row in mounts():
        prefix=row['prefix']
        if url!=prefix and not url.startswith(prefix+'/'):continue
        if write and row['mode']!='read_write':raise PermissionError('VFS mapping is read-only')
        root=row['root']
        if not root.is_dir() or _link(root):raise ValueError('Endpoint VFS root unavailable')
        base=root.resolve(strict=True);p=base
        tail=url[len(prefix):].lstrip('/')
        for part in _segments(tail) if tail else []:
            p=p/part
            if _link(p):raise ValueError('Linked VFS paths are not supported')
        if not p.resolve().is_relative_to(base):raise ValueError('VFS path leaves mapping')
        return p
    raise ValueError('No endpoint-local mapping for VFS URI')

def mappings():
    return [{'prefix':r['prefix'],'mode':r['mode'],'available':r['root'].is_dir() and not _link(r['root'])} for r in mounts()]

def open_input(url):
    p=resolve(url)
    if not p.is_file():raise ValueError('VFS input is not a file')
    return p.open('rb')

def publish(source,url):
    """Create-only, verified local bytes. No cloud durability claim."""
    import hashlib
    p=resolve(url,write=True);src=Path(source)
    if not src.is_file() or _link(src):raise ValueError('Source must be a regular file')
    if not p.parent.is_dir():raise ValueError('Destination directory must already exist')
    if p.exists() or _link(p):raise FileExistsError('VFS output already exists')
    fd,tmp=tempfile.mkstemp(prefix='.pylon-output-',dir=p.parent);digest=hashlib.sha256();size=0
    try:
        with os.fdopen(fd,'wb') as out,src.open('rb') as inp:
            while True:
                b=inp.read(1024*1024)
                if not b:break
                out.write(b);digest.update(b);size+=len(b)
            out.flush();os.fsync(out.fileno())
        # Atomic create-only publication; if filesystem cannot hardlink, refuse.
        os.link(tmp,p)
        with p.open('rb') as f:
            actual=hashlib.file_digest(f,'sha256').hexdigest()
        if actual!=digest.hexdigest():raise IOError('Published file checksum mismatch; inspect destination')
        manifest=os.environ.get('PYLON_WORK_VFS_MANIFEST')
        staged=bool(manifest and p.parent==Path(manifest).parent/'proxy')
        return {'ok':True,'url':url,'bytes':size,'sha256':actual,'durability':'proxy_staged' if staged else 'endpoint_local_verified'}
    finally:Path(tmp).unlink(missing_ok=True)

def clean_progress(value):
    if not isinstance(value,dict):raise ValueError('Invalid progress')
    done,total=value.get('done'),value.get('total');detail=value.get('detail','')
    for v in (done,total):
        if v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1e15):raise ValueError('Invalid progress counter')
    if total is not None and (total<=0 or done is not None and done>total):raise ValueError('Invalid total')
    if not isinstance(detail,str) or len(detail)>200 or any(ord(c)<32 for c in detail):raise ValueError('Invalid progress detail')
    if re.search(r'(?i)(token|password|secret|api.?key|authorization|cookie)\s*[=:]',detail):raise ValueError('Do not report secrets')
    return {'done':done,'total':total,'detail':detail}

def report(done=None,total=None,detail=''):
    path=os.environ.get('PYLON_WORK_PROGRESS_FILE')
    if not path:raise RuntimeError('Not running inside a Pylon workload')
    row=clean_progress({'done':done,'total':total,'detail':detail})
    p=Path(path);fd,tmp=tempfile.mkstemp(prefix='.progress-',dir=p.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(row,f)
        os.replace(tmp,p)
    finally:Path(tmp).unlink(missing_ok=True)

def read_progress(p):
    try:
        if _link(p) or not p.is_file() or p.stat().st_size>MAX_PROGRESS:return None
        with p.open('rb') as f:raw=f.read(MAX_PROGRESS+1)
        if len(raw)>MAX_PROGRESS:return None
        return clean_progress(json.loads(raw))
    except (OSError,ValueError,TypeError):return None

def python_spec(value):
    """Immutable, bounded Python source with exact runtime/package requirements."""
    import hashlib
    if not isinstance(value,dict) or set(value)!={'source','sha256','python','packages','argv'}:raise ValueError('Invalid Python work definition')
    source=value['source'];version=value['python'];packages=value['packages'];argv=value['argv']
    if not isinstance(source,str) or not 1<=len(source.encode('utf-8'))<=16384 or '\x00' in source:raise ValueError('Python source exceeds budget')
    if hashlib.sha256(source.encode('utf-8')).hexdigest()!=value['sha256']:raise ValueError('Python source digest mismatch')
    try:compile(source,'work.py','exec')
    except SyntaxError:raise ValueError('Invalid Python syntax') from None
    if not isinstance(version,str) or not re.fullmatch(r'3\.\d{1,2}',version):raise ValueError('Specify Python major.minor')
    if not isinstance(packages,dict) or len(packages)>32 or any(not isinstance(k,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',k) or not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_.+!-]{1,80}',v) for k,v in packages.items()):raise ValueError('Use exact package versions')
    if not isinstance(argv,list) or len(argv)>32 or any(not isinstance(x,str) or len(x)>1024 or '\x00' in x for x in argv):raise ValueError('Invalid Python arguments')
    if re.search(r'(?i)(token|password|secret|api.?key|authorization|cookie)\s*[=:]',source+' '+' '.join(argv)):raise ValueError('Do not put credentials in work source or arguments')
    return value

def check_python(value):
    import sys,importlib.metadata
    spec=python_spec(value)
    if spec['python']!=f'{sys.version_info.major}.{sys.version_info.minor}':raise ValueError('Endpoint Python version mismatch')
    for name,version in spec['packages'].items():
        try:actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:raise ValueError('Required package missing') from None
        if actual!=version:raise ValueError('Required package version mismatch')
