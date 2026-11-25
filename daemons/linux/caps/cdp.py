"""Linux Chromium CDP provider. Private, temporary profile; no public debug port.

Starts lazily, uses an installed browser (no downloads), and retains the browser
only for the daemon lifetime. The owner-authorized capability does not automate
Google login or MFA. No implicit Windows profile/credential reuse.
"""
from __future__ import annotations
import json
import fcntl
import math
import secrets
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
ATTRS = {'managed': True, 'headless': True, 'persistent_profile': True, 'interactive_viewer': True}
_LOCK = threading.RLock()
_PROC = None
_PROFILE = None
_PORT = None
_LAST = 0.0
_ACTIVE_NAME = None
_PROFILE_LOCK = None
_VIEWER = None
_WIDTH,_HEIGHT = 1200,800
_PROFILE_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_RESERVE = 1024**3
_PROFILE_BUDGET = 256*1024**2
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

def _profile_name(name):
    if name is None or name=='temporary':return None
    if not isinstance(name,str) or not _PROFILE_NAME.fullmatch(name):raise ValueError('Invalid profile name')
    return name

def _root():
    return Path(os.environ.get('PYLON_LINUX_CDP_PROFILE_ROOT') or Path.home()/'.local/share/pylon-server/cdp-profiles')

def _free(path):
    while not path.exists():path=path.parent
    return shutil.disk_usage(path).free

def _persistent_space(path,starting=False):
    floor=max(_RESERVE,int(float(os.environ.get('PYLON2_DISK_FLOOR_GB','1'))*1024**3))
    return _free(path)>=floor+(_PROFILE_BUDGET if starting else 64*1024**2)

def _persistent(name):
    root=_root()
    if not root.is_absolute() or root.resolve()!=root:raise ValueError('Invalid private profile root')
    if not _persistent_space(root,True):raise ValueError('Persistent profile needs at least 1 GiB free reserve plus 256 MiB for browser data')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    for p in (root,root/name):
        p.mkdir(mode=0o700,exist_ok=True)
        st=p.stat()
        if p.is_symlink() or p.resolve()!=p or st.st_uid!=os.getuid() or st.st_mode&0o077:raise ValueError('Profile must be owner-only and not a symlink')
    path=root/name
    fd=os.open(path/'.pylon-profile.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:os.close(fd);raise ValueError('Profile is already in use') from None
    return path,fd

def _stop():
    global _PROC,_PROFILE,_PORT,_ACTIVE_NAME,_PROFILE_LOCK,_VIEWER
    p, profile, name, lock = _PROC,_PROFILE,_ACTIVE_NAME,_PROFILE_LOCK
    if p and name and p.poll() is None:
        try:
            _command(_http('/json/version'),'Browser.close',{})
        except Exception:pass
        try:p.wait(timeout=4)
        except subprocess.TimeoutExpired:pass
    _PROC=_PROFILE=_PORT=_ACTIVE_NAME=_PROFILE_LOCK=_VIEWER=None
    if p:
        try: os.killpg(p.pid,signal.SIGTERM)
        except ProcessLookupError: pass
        try:p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            p.wait(timeout=3)
    if profile and name is None:shutil.rmtree(profile,ignore_errors=True)
    if lock is not None:os.close(lock)

def shutdown():
    with _LOCK:_stop()

def _reap():
    with _LOCK:
        try:
            if _PROC and (_PROC.poll() is not None or time.monotonic()-_LAST>_IDLE or (_ACTIVE_NAME and not _persistent_space(_PROFILE))):_stop()
        except Exception:
            if _PROC:_stop()
    timer=threading.Timer(30,_reap);timer.daemon=True;timer.start()

def _ensure(profile=None):
    global _PROC,_PROFILE,_PORT,_LAST,_ACTIVE_NAME,_PROFILE_LOCK
    name=_profile_name(profile)
    if _PROC and _PROC.poll() is None and _ACTIVE_NAME!=name:raise ValueError("Another browser profile is active; stop it explicitly first")
    if _PROC and _PROC.poll() is None:
        try:
            _http('/json/version');_LAST=time.monotonic();return
        except Exception: _stop()
    elif _PROC:_stop()
    executable=_browser()
    if not executable:raise RuntimeError('Install Chromium or configure PYLON_LINUX_CDP_BROWSER')
    # Keep volatile browser data out of the VM's critically small boot disk.
    if name:
        profile,lock=_persistent(name);_PROFILE_LOCK=lock
    else:
        base='/dev/shm' if Path('/dev/shm').is_dir() else None
        if base and shutil.disk_usage(base).free<128*1024*1024:raise RuntimeError('Insufficient temporary browser storage')
        profile=Path(tempfile.mkdtemp(prefix='pylon-linux-cdp-',dir=base))
    _PROFILE=profile;_ACTIVE_NAME=name
    try:
        _PROC=subprocess.Popen([executable,'--headless','--remote-debugging-address=127.0.0.1',
          '--remote-debugging-port=0','--user-data-dir='+str(profile),'--disk-cache-size=16777216',
          '--window-size=1200,800','--disable-background-networking','--no-first-run','--no-default-browser-check','about:blank'],
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
        return {'ok':True,'available':available,'running':running,'managed':True,'headless':True,'persistent_profile':True,'profile':_ACTIVE_NAME or 'temporary','persistent_ready':_persistent_space(_root(),True),'browser':'Chromium','detail':'Starts on demand; temporary or named profile; interactive viewer available' if available else 'Chromium executable not found'}

def _automation(args):
    if _VIEWER:raise ValueError("Interactive viewer active; close it before automation")
    _ensure(args.get("profile"))

def tabs(args,ctx):
    with _LOCK:
        _automation(args)
        return {'ok':True,'tabs':[{k:t.get(k,'') for k in ('id','type','title','url')} for t in _tabs()]}

def navigate(args,ctx):
    url=_url(args.get('url'))
    with _LOCK:
        _automation(args)
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
        _automation(args)
        t=_target(args.get('tab_id'))
        r=_command(t,'Runtime.evaluate',{'expression':expr,'returnByValue':True,'awaitPromise':True,'timeout':10000})
        if 'exceptionDetails' in r:return _error('Browser evaluation failed')
        return {'ok':True,'value':r.get('result',{}).get('value')}

def close(args,ctx):
    global _LAST
    with _LOCK:
        if _VIEWER: return _error('Interactive viewer active')
        if not _PROC or _PROC.poll() is not None:return _error('Browser not running')
        if _profile_name(args.get('profile'))!=_ACTIVE_NAME:return _error('Profile selection required')
        t=_target(args.get('tab_id'))
        request=urllib.request.Request('http://127.0.0.1:'+str(_PORT)+'/json/close/'+t['id'])
        with _OPENER.open(request,timeout=4) as r:r.read(1024)
        _LAST=time.monotonic()
        return {'ok':True}

def stop(args,ctx):
    if args.get('confirm') is not True:return {'ok':True,'dry_run':True}
    with _LOCK:
        if _VIEWER:return _error('Close the active viewer first')
        _stop()
    return {'ok':True}

def _viewer(args,ctx):
    if not _VIEWER or not isinstance(args.get('session'),str) or not secrets.compare_digest(args['session'],_VIEWER['session']) or ctx.get('user')!=_VIEWER['owner']:
        raise ValueError('Viewer session expired')
    _ensure(_VIEWER['profile'])
    if not _VIEWER:raise ValueError('Viewer browser restarted; reopen viewer')
    return _target(_VIEWER['tab'])

def viewer_open(args,ctx):
    global _VIEWER
    if args.get('confirm') is not True or not ctx.get('user'):raise ValueError('Explicit owner confirmation required')
    url=_url(args.get('url'));name=_profile_name(args.get('profile'))
    with _LOCK:
        if _VIEWER:raise ValueError('A viewer is already active')
        _ensure(name)
        if len(_tabs())>=_MAX_TABS:raise ValueError('Browser tab limit reached')
        tab=_http('/json/new?'+quote(url,safe=''),method='PUT')
        _command(tab,'Emulation.setDeviceMetricsOverride',{'width':_WIDTH,'height':_HEIGHT,'deviceScaleFactor':1,'mobile':False})
        sid=secrets.token_urlsafe(32)
        _VIEWER={'session':sid,'owner':ctx['user'],'tab':tab['id'],'profile':name}
        return {'ok':True,'session':sid,'profile':name or 'temporary'}

def viewer_shot(args,ctx):
    with _LOCK:
        t=_viewer(args,ctx)
        r=_command(t,'Page.captureScreenshot',{'format':'jpeg','quality':55,'captureBeyondViewport':False})
        # Never return page text, cookies or a query-bearing URL.
        u=urlsplit(t.get('url',''));origin=u.scheme+'://'+u.hostname if u.hostname else 'about:blank'
        return {'ok':True,'img':r['data'],'w':_WIDTH,'h':_HEIGHT,'url':origin}

def _number(value,low,high):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError('Invalid viewer input')
    return value

def viewer_input(args,ctx):
    with _LOCK:
        t=_viewer(args,ctx);kind=args.get('kind');p=args.get('payload')
        if not isinstance(p,dict):raise ValueError('Invalid viewer input')
        if kind=='type':
            text=p.get('text')
            if not isinstance(text,str) or len(text)>2048:raise ValueError('Invalid text input')
            _command(t,'Input.insertText',{'text':text})
        elif kind in ('click','move','wheel'):
            x=_number(p.get('xf'),0,1)*(_WIDTH-1);y=_number(p.get('yf'),0,1)*(_HEIGHT-1)
            q={'x':x,'y':y}
            if kind=='click':
                for event in ('mousePressed','mouseReleased'):_command(t,'Input.dispatchMouseEvent',dict(q,type=event,button='left',clickCount=1))
            elif kind=='move':_command(t,'Input.dispatchMouseEvent',dict(q,type='mouseMoved'))
            else:_command(t,'Input.dispatchMouseEvent',dict(q,type='mouseWheel',deltaX=_number(p.get('deltaX',0),-3000,3000),deltaY=_number(p.get('deltaY',0),-3000,3000)))
        elif kind=='key':
            key=p.get('key');mod=p.get('modifiers',0)
            allowed={'Backspace':8,'Tab':9,'Enter':13,'Escape':27,'PageUp':33,'PageDown':34,'End':35,'Home':36,'ArrowLeft':37,'ArrowUp':38,'ArrowRight':39,'ArrowDown':40,'Delete':46}
            if key not in allowed or not isinstance(mod,int) or not 0<=mod<=15:raise ValueError('Unsupported viewer key')
            q={'key':key,'windowsVirtualKeyCode':allowed[key],'modifiers':mod}
            _command(t,'Input.dispatchKeyEvent',dict(q,type='keyDown',**({'text':'\r'} if key=='Enter' else {})))
            _command(t,'Input.dispatchKeyEvent',dict(q,type='keyUp'))
        else:raise ValueError('Unsupported viewer input')
        return {'ok':True,'applied':True}

def viewer_recipe(args,ctx):
    """Trusted server supplies a fixed recipe, never a UI-provided expression.
    Session ownership and the same tab are checked before evaluation.
    Only whitelisted status/field names cross the viewer boundary.
    """
    expr=args.get('expr')
    if args.get('confirm') is not True or not isinstance(expr,str) or not 1<=len(expr)<=65536:
        return _error('Recipe confirmation required')
    with _LOCK:
        t=_viewer(args,ctx)
        r=_command(t,'Runtime.evaluate',{'expression':expr,'returnByValue':True,'awaitPromise':True,'timeout':10000})
        v=r.get('result',{}).get('value')
        states={'sign_in_required','outside_setup','open_create_form','review_required','existing_values','select_web_type','form_ready','prepared'}
        if 'exceptionDetails' in r or not isinstance(v,dict) or v.get('state') not in states:
            return _error('Setup page not recognized')
        fields=v.get('fields',[])
        return {'ok':True,'state':v['state'],'fields':[f for f in fields if isinstance(f,str) and f in ('name','redirect_uri','web_type')] if isinstance(fields,list) else []}

def viewer_stop(args,ctx):
    with _LOCK:
        _viewer(args,ctx)
        _stop() # Flush persistent profile by normal Chromium shutdown. Temporary profile is removed.
        return {'ok':True}

def _guard(fn):
    def run(args,ctx):
        try:return fn(args,ctx)
        except ValueError as e:return _error('Invalid or expired viewer input' if fn is viewer_input else str(e))
        except Exception:return _error('Linux Chromium unavailable or browser operation failed')
    return run

METHODS={k:_guard(v) for k,v in {'status':status,'tabs':tabs,'navigate':navigate,'eval':evaluate,'close':close,'stop':stop,'viewer_open':viewer_open,'viewer_shot':viewer_shot,'viewer_input':viewer_input,'viewer_stop':viewer_stop,'viewer_recipe':viewer_recipe}.items()}
# Start only the inexpensive idle monitor, not Chromium, on daemon import.
_timer=threading.Timer(30,_reap);_timer.daemon=True;_timer.start()
