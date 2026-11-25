"""Linux Chromium CDP provider. Private, temporary profile; no public debug port.

Starts lazily, uses an installed browser (no downloads), and retains the browser
only for the daemon lifetime. The owner-authorized capability does not automate
Google login or MFA. No implicit Windows profile/credential reuse.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, quote
from websockets.sync.client import connect

CAPABILITY = 'cap:system.cdp'
VARIANT = 'headless'
ATTRS = {'managed': True, 'headless': True, 'persistent_profile': False}
_LOCK = threading.RLock()
_PROC = None
_PROFILE = None
_PORT = None
_LAST = 0.0
_IDLE = 300
_MAX_TABS = 4
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')

def _browser():
    override = os.environ.get('PYLON_LINUX_CDP_BROWSER')
    if override:
        p = Path(override)
        return str(p) if p.is_absolute() and p.is_file() and os.access(p, os.X_OK) else None
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'msedge'):
        p = shutil.which(name)
        if p: return p
    cache = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or Path.home()/'.cache/ms-playwright')
    if cache.is_absolute():
        candidates = sorted(cache.glob('chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'), reverse=True)
        return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    return None

def _error(message, category='application'):
    return {'ok':False,'error':message,'error_category':category}

def _http(path, method='GET'):
    if not _PORT: raise RuntimeError('Browser not running')
    request=urllib.request.Request('http://127.0.0.1:'+str(_PORT)+path,method=method)
    with _OPENER.open(request,timeout=4) as r:
        raw=r.read(2*1024*1024+1)
    if len(raw)>2*1024*1024: raise RuntimeError('CDP response exceeds limit')
    return json.loads(raw)

def _stop():
    global _PROC,_PROFILE,_PORT
    p, profile = _PROC, _PROFILE
    _PROC=_PROFILE=_PORT=None
    if p:
        try: os.killpg(p.pid,signal.SIGTERM)
        except ProcessLookupError: pass
        try:p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            p.wait(timeout=3)
    if profile:shutil.rmtree(profile,ignore_errors=True)

def shutdown():
    with _LOCK:_stop()

def _reap():
    with _LOCK:
        if _PROC and (_PROC.poll() is not None or time.monotonic()-_LAST>_IDLE):_stop()
    timer=threading.Timer(30,_reap);timer.daemon=True;timer.start()

def _ensure():
    global _PROC,_PROFILE,_PORT,_LAST
    if _PROC and _PROC.poll() is None:
        try:
            _http('/json/version');_LAST=time.monotonic();return
        except Exception: _stop()
    elif _PROC:_stop()
    executable=_browser()
    if not executable:raise RuntimeError('Install Chromium or configure PYLON_LINUX_CDP_BROWSER')
    # Keep volatile browser data out of the VM's critically small boot disk.
    base='/dev/shm' if Path('/dev/shm').is_dir() else None
    if base and shutil.disk_usage(base).free<128*1024*1024:raise RuntimeError('Insufficient temporary browser storage')
    profile=Path(tempfile.mkdtemp(prefix='pylon-linux-cdp-',dir=base));_PROFILE=profile
    try:
        _PROC=subprocess.Popen([executable,'--headless','--remote-debugging-address=127.0.0.1',
          '--remote-debugging-port=0','--user-data-dir='+str(profile),'--disk-cache-size=16777216',
          '--disable-background-networking','--no-first-run','--no-default-browser-check','about:blank'],
          stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if _PROC.poll() is not None:raise RuntimeError('Chromium exited; check browser sandbox and dependencies')
            try:
                port=int((profile/'DevToolsActivePort').read_text().splitlines()[0])
                if not 1<=port<=65535:raise ValueError()
                _PORT=port;_http('/json/version');_LAST=time.monotonic();return
            except (OSError,ValueError,IndexError):time.sleep(.15)
        raise RuntimeError('Chromium startup timed out')
    except BaseException:_stop();raise

def _tabs():
    return [t for t in _http('/json/list') if t.get('type')=='page']

def _target(id):
    if not isinstance(id,str) or not _ID.fullmatch(id):raise ValueError('Explicit tab_id required')
    t=next((t for t in _tabs() if t.get('id')==id),None)
    if not t:raise ValueError('Selected tab unavailable')
    return t

def _command(t,method,params):
    u=urlsplit(t['webSocketDebuggerUrl'])
    if u.scheme!='ws' or u.hostname!='127.0.0.1' or u.port!=_PORT or u.username or u.password:
        raise RuntimeError('Unexpected browser debug target')
    with connect(t['webSocketDebuggerUrl'],open_timeout=4,close_timeout=1,max_size=2*1024*1024,proxy=None) as ws:
        ws.send(json.dumps({'id':1,'method':method,'params':params}))
        deadline=time.monotonic()+12
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RuntimeError('Browser operation timed out')
            r=json.loads(ws.recv(timeout=remaining))
            if r.get('id')==1:
                if r.get('error'):raise RuntimeError('Browser rejected CDP command')
                return r.get('result',{})

def _url(value):
    if not isinstance(value,str) or len(value)>8192:raise ValueError('HTTP(S) URL required')
    u=urlsplit(value)
    if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:raise ValueError('HTTP(S) URL required')
    return value

def status(args,ctx):
    with _LOCK:
        available=bool(_browser())
        running=bool(_PROC and _PROC.poll() is None)
        return {'ok':True,'available':available,'running':running,'managed':True,'headless':True,'persistent_profile':False,'browser':'Chromium','detail':'Starts on demand; temporary profile; user sign-in requires a separate interactive browser path' if available else 'Chromium executable not found'}

def tabs(args,ctx):
    with _LOCK:
        _ensure()
        return {'ok':True,'tabs':[{k:t.get(k,'') for k in ('id','type','title','url')} for t in _tabs()]}

def navigate(args,ctx):
    url=_url(args.get('url'))
    with _LOCK:
        _ensure()
        if args.get('tab_id'):
            t=_target(args['tab_id'])
            r=_command(t,'Page.navigate',{'url':url})
            if r.get('errorText'):return _error('Browser navigation failed')
        else:
            if len(_tabs())>=_MAX_TABS:return _error('Browser tab limit reached; close an existing tab')
            t=_http('/json/new?'+quote(url,safe=''),method='PUT')
        return {'ok':True,'tab_id':t['id']}

def evaluate(args,ctx):
    expr=args.get('expr')
    if not isinstance(expr,str) or not expr.strip() or len(expr)>65536:return _error('Invalid expression')
    with _LOCK:
        _ensure()
        t=_target(args.get('tab_id'))
        r=_command(t,'Runtime.evaluate',{'expression':expr,'returnByValue':True,'awaitPromise':True,'timeout':10000})
        if 'exceptionDetails' in r:return _error('Browser evaluation failed')
        return {'ok':True,'value':r.get('result',{}).get('value')}

def close(args,ctx):
    global _LAST
    with _LOCK:
        if not _PROC or _PROC.poll() is not None:return _error('Browser not running')
        t=_target(args.get('tab_id'))
        request=urllib.request.Request('http://127.0.0.1:'+str(_PORT)+'/json/close/'+t['id'])
        with _OPENER.open(request,timeout=4) as r:r.read(1024)
        _LAST=time.monotonic()
        return {'ok':True}

def stop(args,ctx):
    if args.get('confirm') is not True:return {'ok':True,'dry_run':True}
    shutdown()
    return {'ok':True}

def _guard(fn):
    def run(args,ctx):
        try:return fn(args,ctx)
        except ValueError as e:return _error(str(e))
        except Exception:return _error('Linux Chromium unavailable or browser operation failed')
    return run

METHODS={k:_guard(v) for k,v in {'status':status,'tabs':tabs,'navigate':navigate,'eval':evaluate,'close':close,'stop':stop}.items()}
# Start only the inexpensive idle monitor, not Chromium, on daemon import.
_timer=threading.Timer(30,_reap);_timer.daemon=True;_timer.start()
