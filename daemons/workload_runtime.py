"""Bounded endpoint-local compute jobs. Survive reverse-channel disconnection,
not daemon/process restart. Durable receipts prevent blind replay.
Commands are explicit owner-authorized shell work; outputs must not contain secrets.
"""
import base64,hashlib,json,os,re,shutil,signal,subprocess,sys,threading,time,uuid
from pathlib import Path

try:
    from daemons import work_sdk
except ImportError:
    import work_sdk

ROOT=Path.home()/'.pylon2'/'endpoint-workloads'
LOCK=threading.RLock()
ACTIVE={}
MAX_ACTIVE=1
MAX_RECORDS=256
TAIL=16384

def path(jid):
    if not isinstance(jid,str) or not re.fullmatch(r'[a-f0-9]{32}',jid):raise ValueError('Invalid workload ID')
    return ROOT/jid/'receipt.json'

def save(p,row):
    tmp=p.with_name('.receipt-'+uuid.uuid4().hex)
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(row,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:tmp.unlink(missing_ok=True)

def load(p):
    if not p.is_file():return None
    return json.loads(p.read_text(encoding='utf-8'))

def status(args,ctx):
    p=path(args.get('job_id'))
    with LOCK:
        row=load(p)
        if row is None:return {'ok':True,'status':'unknown'}
        if row['status'] in ('queued','running','stop_requested') and row['id'] not in ACTIVE:
            row.update(status='interrupted',finished_at=time.time());save(p,row)
        result={k:v for k,v in row.items() if k!='fingerprint'}
        progress=work_sdk.read_progress(p.parent/'progress.json')
        if progress is not None:
            result['progress']=progress
            try:result['progress_age_s']=max(0,round(time.time()-(p.parent/'progress.json').stat().st_mtime,1))
            except OSError:pass
        return {'ok':True,'job':result}

def terminate(proc):
    if os.name=='nt':
        # Only terminate the owned child process tree while its root is alive.
        if proc.poll() is None:
            try:subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except (OSError,subprocess.TimeoutExpired):pass
        if proc.poll() is None:proc.kill()
    else:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass

def _run(p,row,cmd,cwd,timeout,stop,python=None):
    proc=None;readers=[];tails=[bytearray(),bytearray()];counts=[0,0];started=time.monotonic()
    def drain(stream,i):
        with stream:
            while True:
                chunk=stream.read(65536)
                if not chunk:break
                counts[i]+=len(chunk);tails[i].extend(chunk)
                if len(tails[i])>TAIL:del tails[i][:-TAIL]
    try:
        with LOCK:
            if stop.is_set():
                row.update(status='cancelled');return
            row.update(status='running',started_at=time.time());save(p,row)
        if python is not None:
            work_sdk.check_python(python)
            if hashlib.sha256((p.parent/'work.py').read_bytes()).hexdigest()!=python['sha256']:raise ValueError('Staged source mismatch')
            argv=[sys.executable,str(p.parent/'work.py'),*python['argv']]
        else:argv=[os.environ.get('COMSPEC','cmd.exe'),'/d','/c',cmd] if os.name=='nt' else ['/bin/bash','-c',cmd]
        # Do not pass daemon authentication/configuration variables to a child.
        allowed={'PATH','HOME','USER','LOGNAME','USERPROFILE','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','TMPDIR','LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA','LANG','PATHEXT','HOMEDRIVE','HOMEPATH','NUMBER_OF_PROCESSORS'}
        env={k:v for k,v in os.environ.items() if k.upper() in allowed or k.upper().startswith('LC_')}
        env['PYTHONIOENCODING']='utf-8'
        env['PYTHONPATH']=str(Path(work_sdk.__file__).resolve().parent)
        env['PYLON_WORK_PROGRESS_FILE']=str(p.parent/'progress.json')
        if (p.parent/'vfs.json').is_file():env['PYLON_WORK_VFS_MANIFEST']=str(p.parent/'vfs.json')
        proc=subprocess.Popen(argv,cwd=cwd or p.parent,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
             **({'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)} if os.name=='nt' else {'start_new_session':True}))
        readers=[threading.Thread(target=drain,args=(s,i),daemon=True) for i,s in enumerate((proc.stdout,proc.stderr))]
        for t in readers:t.start()
        outcome='completed'
        while proc.poll() is None:
            if stop.wait(.2):outcome='cancelled';terminate(proc);break
            if time.monotonic()-started>=timeout:outcome='timed_out';terminate(proc);break
        proc.wait(timeout=15)
        if os.name!='nt':terminate(proc) # clean up same-process-group children after root exits
        for t in readers:t.join(2)
        if any(t.is_alive() for t in readers):outcome='interrupted'
        if outcome=='completed' and proc.returncode!=0:outcome='failed'
        row.update(status=outcome,exit_code=proc.returncode,stdout=bytes(tails[0]).decode('utf-8','replace'),stderr=bytes(tails[1]).decode('utf-8','replace'),truncated=any(n>TAIL for n in counts))
    except Exception:
        if proc is not None:
            try:terminate(proc);proc.wait(timeout=15)
            except Exception:pass
        row.update(status='interrupted' if proc is not None else 'failed')
    finally:
        with LOCK:
            row.update(finished_at=time.time(),duration_ms=int((time.monotonic()-started)*1000))
            try:save(p,row)
            finally:ACTIVE.pop(row['id'],None)

def stream_manifest(value):
    if not isinstance(value,dict) or len(value)>32:raise ValueError('Invalid stream manifest')
    total=0
    for k,v in value.items():
        if not isinstance(k,str) or not re.fullmatch(r'(?:[0-9]|[12][0-9]|3[01])',k) or not isinstance(v,dict) or set(v)!={'bytes','sha256'} or type(v['bytes']) is not int or not 0<=v['bytes']<=work_sdk.STREAM_FILE or not isinstance(v['sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',v['sha256']):raise ValueError('Invalid stream file')
        total+=v['bytes']
    if total>work_sdk.STREAM_TOTAL:raise ValueError('Stream batch exceeds quota')
    return value

def stream_dir(jid):
    return path(jid).parent.with_name('.stage-'+jid)

def stream_begin(args,ctx):
    jid=args.get('job_id');p=stream_dir(jid);m=stream_manifest(args.get('files'))
    with LOCK:
        if path(jid).parent.exists():raise ValueError('Job already exists')
        ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
        if p.exists():
            r=load(p/'manifest.json')
            if not r or r['files']!=m or r['expires']<time.time():raise ValueError('Existing transfer mismatch or expired')
        else:
            if sum(1 for x in ROOT.glob('.stage-*') if x.is_dir())>=8:raise ValueError('Staging capacity exhausted; inspect expired transfers')
            if shutil.disk_usage(ROOT).free-sum(x['bytes'] for x in m.values())<512*1024*1024:raise ValueError('Endpoint disk reserve')
            p.mkdir(mode=0o700)
            save(p/'manifest.json',{'files':m,'expires':time.time()+3600})
            for k in m:(p/k).touch(exist_ok=False)
        return {'ok':True,'offsets':{k:(p/k).stat().st_size for k in m}}

def stream_chunk(args,ctx):
    p=stream_dir(args.get('job_id'));key=str(args.get('index'));offset=args.get('offset');encoded=args.get('b64')
    if type(offset) is not int or offset<0 or not isinstance(encoded,str) or len(encoded)>4*((work_sdk.STREAM_CHUNK+2)//3):raise ValueError('Invalid chunk')
    raw=base64.b64decode(encoded,validate=True)
    if not raw or len(raw)>work_sdk.STREAM_CHUNK or hashlib.sha256(raw).hexdigest()!=args.get('sha256'):raise ValueError('Chunk checksum mismatch')
    with LOCK:
        r=load(p/'manifest.json')
        if not r or r['expires']<time.time() or key not in r['files']:raise ValueError('Transfer unavailable')
        target=p/key
        if work_sdk._link(target) or work_sdk._link(p):raise ValueError('Unsafe staging path')
        size=target.stat().st_size
        if offset+len(raw)>r['files'][key]['bytes']:raise ValueError('Chunk exceeds declaration')
        if offset<size:
            with target.open('rb') as f:f.seek(offset);old=f.read(len(raw))
            if old!=raw:raise ValueError('Conflicting repeated chunk')
            return {'ok':True,'offset':size}
        if offset!=size:raise ValueError('Noncontiguous chunk')
        with target.open('ab') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        return {'ok':True,'offset':size+len(raw)}

def stream_validate(jid,m):
    if not m:return
    p=stream_dir(jid);r=load(p/'manifest.json')
    if not r or r['expires']<time.time() or r['files']!=m:raise ValueError('Staging manifest mismatch')
    for key,meta in m.items():
        f=p/key
        if work_sdk._link(f) or f.stat().st_size!=meta['bytes']:raise ValueError('Incomplete staged input')
        with f.open('rb') as inp:
            if hashlib.file_digest(inp,'sha256').hexdigest()!=meta['sha256']:raise ValueError('Staged input checksum mismatch')

def stream_output(args,ctx):
    p=path(args.get('job_id'));idx=args.get('index')
    with LOCK:
        row=load(p);req=load(p.parent/'vfs.json') or []
        if not row or row.get('status')!='completed' or type(idx) is not int or not 0<=idx<len(req) or req[idx]['route']!='proxy_stream' or req[idx]['mode']!='write':raise ValueError('Undeclared stream output')
        f=p.parent/'proxy'/str(idx)
        if work_sdk._link(f) or work_sdk._link(f.parent) or not f.is_file():raise ValueError('Missing output')
        size=f.stat().st_size
        if size>work_sdk.STREAM_FILE:raise ValueError('Stream output exceeds quota')
        if 'offset' not in args:
            with f.open('rb') as inp:digest=hashlib.file_digest(inp,'sha256').hexdigest()
            return {'ok':True,'bytes':size,'sha256':digest}
        offset=args['offset']
        if type(offset) is not int or not 0<=offset<=size:raise ValueError('Invalid output offset')
        with f.open('rb') as inp:inp.seek(offset);raw=inp.read(work_sdk.STREAM_CHUNK)
        return {'ok':True,'offset':offset,'b64':base64.b64encode(raw).decode('ascii'),'sha256':hashlib.sha256(raw).hexdigest()}

def submit(args,ctx):
    p=path(args.get('job_id'));cmd=args.get('cmd');cwd=args.get('cwd');timeout=args.get('timeout',1800)
    python=args.get('python')
    if python is not None:
        if cmd is not None or cwd is not None:raise ValueError('Portable Python work uses its private work directory')
        work_sdk.check_python(python)
    elif not isinstance(cmd,str) or not 1<=len(cmd)<=8192 or '\x00' in cmd or re.search(r'(?i)(token|password|secret|api.?key|authorization|cookie)\s*[=:]',cmd):raise ValueError('Provide a nonsecret command')
    if type(timeout) is not int or not 5<=timeout<=3600:raise ValueError('Invalid workload timeout')
    if cwd is not None and (not isinstance(cwd,str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir()):raise ValueError('Specify an existing absolute endpoint working directory')
    req=work_sdk.requirements(args.get('vfs',[]))
    payload=args.get('proxy_inputs',{})
    if not isinstance(payload,dict):raise ValueError('Invalid staged inputs')
    expected={str(i) for i,r in enumerate(req) if r['route']=='proxy' and r['mode']=='read'}
    if set(payload)!=expected:raise ValueError('Proxy input declaration mismatch')
    staged={};total=0
    for key,value in payload.items():
        if not isinstance(value,dict) or set(value)!={'b64','sha256'} or not isinstance(value['b64'],str) or len(value['b64'])>4*((work_sdk.PROXY_LIMIT+2)//3):raise ValueError('Invalid proxy input')
        raw=base64.b64decode(value['b64'],validate=True);total+=len(raw)
        if len(raw)>work_sdk.PROXY_LIMIT or total>work_sdk.PROXY_TOTAL or hashlib.sha256(raw).hexdigest()!=value['sha256']:raise ValueError('Proxy input size or checksum mismatch')
        staged[key]=raw
    fp=hashlib.sha256(json.dumps([cmd,cwd,timeout]+([req, {k:v['sha256'] for k,v in payload.items()}] if req else []),sort_keys=True).encode()).hexdigest()
    streams=stream_manifest(args.get('stream_inputs',{}))
    if set(streams)!={str(i) for i,r in enumerate(req) if r['route']=='proxy_stream' and r['mode']=='read'}:raise ValueError('Stream declaration mismatch')
    if streams:fp=hashlib.sha256(json.dumps([fp,streams],sort_keys=True).encode()).hexdigest()
    if python is not None:fp=hashlib.sha256(json.dumps([fp,python],sort_keys=True).encode()).hexdigest()
    with LOCK:
        existing=load(p)
        if existing:
            if existing['fingerprint']!=fp:raise ValueError('Workload ID already used for a different command')
            return status(args,ctx)
        work_sdk.check_requirements(req)
        stream_validate(args['job_id'],streams)
        if p.parent.exists():raise ValueError('Workload folder already exists without receipt; review required')
        if len(ACTIVE)>=MAX_ACTIVE:raise ValueError('Endpoint compute slot busy')
        ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
        if shutil.disk_usage(cwd or ROOT).free<512*1024*1024:raise ValueError('Endpoint disk reserve below 512 MiB')
        if sum(1 for x in ROOT.iterdir() if x.is_dir())>=MAX_RECORDS:raise ValueError('Endpoint workload history limit; operator review required')
        p.parent.mkdir(mode=0o700)
        if python is not None:
            (p.parent/'work.py').write_bytes(python['source'].encode('utf-8'))
        if req:
            save(p.parent/'vfs.json',req)
            (p.parent/'proxy').mkdir(mode=0o700)
            for key in streams:os.replace(stream_dir(args['job_id'])/key,p.parent/'proxy'/key)
            if streams:
                (stream_dir(args['job_id'])/'manifest.json').unlink();stream_dir(args['job_id']).rmdir()
            for key,raw in staged.items():
                with (p.parent/'proxy'/key).open('xb') as out:out.write(raw)
        row={'id':args['job_id'],'fingerprint':fp,'status':'queued','created_at':time.time(),'workdir':str(Path(cwd) if cwd else p.parent)}
        save(p,row)
        stop=threading.Event();ACTIVE[row['id']]=stop
        if python is not None:row['source_sha256']=python['sha256'];save(p,row)
        t=threading.Thread(target=_run,args=(p,row,cmd,cwd,timeout,stop,python),daemon=True,name='endpoint-workload')
        t.start()
        return {'ok':True,'job':{'id':row['id'],'status':'queued'}}

def cancel(args,ctx):
    p=path(args.get('job_id'))
    with LOCK:
        stop=ACTIVE.get(args['job_id'])
        if stop:
            stop.set()
            row=load(p)
            if row:row['status']='stop_requested';save(p,row)
    return status(args,ctx)

def capacity_status(args,ctx):
    with LOCK:
        free=shutil.disk_usage(ROOT if ROOT.exists() else ROOT.parent if ROOT.parent.exists() else Path.home()).free
        return {'ok':True,'compute_slots':max(0,MAX_ACTIVE-len(ACTIVE)),'disk_free_bytes':free,'cpu_count':os.cpu_count() or 1,'admission_ready':len(ACTIVE)<MAX_ACTIVE and free>=512*1024*1024}

def python_status(args,ctx):
    try:work_sdk.check_python(args.get('python'))
    except (ValueError,TypeError,SyntaxError):return {'ok':True,'ready':False}
    return {'ok':True,'ready':True}

def vfs_status(args,ctx):
    if 'vfs' in args:
        try:work_sdk.check_requirements(args['vfs'])
        except (ValueError,OSError):return {'ok':True,'ready':False}
        return {'ok':True,'ready':True}
    return {'ok':True,'mappings':work_sdk.mappings()}

def vfs_output(args,ctx):
    p=path(args.get('job_id'));index=args.get('index')
    with LOCK:
        row=load(p)
        if not row or row.get('status')!='completed':raise ValueError('Completed job required')
        req=load(p.parent/'vfs.json') or []
        if type(index) is not int or not 0<=index<len(req) or req[index]['route']!='proxy' or req[index]['mode']!='write':raise ValueError('Undeclared output')
        target=p.parent/'proxy'/str(index)
        if work_sdk._link(target.parent) or work_sdk._link(target) or not target.is_file():raise ValueError('Missing or invalid output')
        with target.open('rb') as f:raw=f.read(work_sdk.PROXY_LIMIT+1)
        if len(raw)>work_sdk.PROXY_LIMIT:raise ValueError('Proxy output exceeds byte limit')
        return {'ok':True,'b64':base64.b64encode(raw).decode('ascii'),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}

METHODS={'submit':submit,'status':status,'cancel':cancel,'vfs_status':vfs_status,'vfs_output':vfs_output,'python_status':python_status,'stream_begin':stream_begin,'stream_chunk':stream_chunk,'stream_output':stream_output,'capacity_status':capacity_status}
