"""Durable VM jobs owned by independent user systemd services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def publish(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


class Jobs:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, job_id):
        if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('Invalid job id')
        path = self.root / job_id
        if not path.is_dir():
            raise ValueError('Unknown job')
        return path

    def submit(self, payload, principal, session_id):
        job_id = uuid.uuid4().hex
        path = self.root / job_id
        path.mkdir(mode=0o700)
        publish(path/'input.json', dict(payload, principal=principal, session_id=session_id))
        publish(path/'status.json', {'ok':True, 'job_id':job_id, 'status':'queued', 'submitted':time.time()})
        result = subprocess.run(['systemd-run','--user','--quiet','--collect','--unit=pylon-mcp-job-'+job_id, '--working-directory='+str(Path(__file__).resolve().parent.parent),
            sys.executable, '-m', 'googlevm_mcp.jobs', str(path)], capture_output=True, text=True)
        if result.returncode:
            publish(path/'status.json', {'ok':False, 'job_id':job_id, 'status':'failed', 'error':result.stderr})
            raise RuntimeError('Could not start background service: '+result.stderr)
        return {'ok':True, 'job_id':job_id, 'status':'queued'}

    def get(self, job_id, principal, offset=0, length=65536):
        path = self.path(job_id)
        payload = json.loads((path/'input.json').read_text())
        if payload['principal'] != principal:
            raise ValueError('Job belongs to another client')
        status = json.loads((path/'status.json').read_text())
        if offset < 0 or length < 0:
            raise ValueError('offset/length must be nonnegative')
        result = path/'result.json'
        status.update(result_path=str(result), session_id=payload.get('session_id',''))
        if result.exists():
            with result.open('rb') as stream:
                stream.seek(offset)
                content = stream.read(length or -1)
            import base64
            status.update(content_b64=base64.b64encode(content).decode(), offset=offset,
                          next_offset=offset+len(content), total_bytes=result.stat().st_size)
        elif status['status'] in ('queued','running'):
            active = subprocess.run(['systemctl','--user','is-active','pylon-mcp-job-'+job_id],capture_output=True,text=True)
            if active.stdout.strip() not in ('active','activating'):
                status.update(ok=False,status='interrupted',error='Job service stopped without a result')
        return status

    def cancel(self, job_id, principal):
        self.get(job_id, principal)
        result = subprocess.run(['systemctl','--user','stop','pylon-mcp-job-'+job_id],capture_output=True,text=True)
        path = self.path(job_id)
        status = json.loads((path/'status.json').read_text())
        if status['status'] in ('queued','running'):
            publish(path/'status.json',dict(status,status='cancelled',ok=False))
        return {'ok':result.returncode==0,'job_id':job_id,'status':json.loads((path/'status.json').read_text())['status']}


def worker(path):
    from .runtime import Runtime
    from .runner import execute
    path = Path(path)
    payload = json.loads((path/'input.json').read_text())
    status = json.loads((path/'status.json').read_text())
    publish(path/'status.json',dict(status,status='running',started=time.time()))
    try:
        if payload['kind']=='command':
            from .host_caps import terminal
            result = terminal.run(payload['arguments'], {'unrestricted':True})
        elif payload['kind']=='youtube':
            from .youtube import capture
            result = capture(payload['url'],payload['data_root'],payload.get('backend','auto'),payload.get('max_comments',100))
        else:
            result = execute(payload['version'],payload['arguments'],payload['data_root'],True)
        publish(path/'result.json',result)
        publish(path/'status.json',dict(status,ok=bool(result.get('ok')),status='done' if result.get('ok') else 'failed',finished=time.time()))
        if payload.get('session_id'):
            records = Runtime(payload['runtime_dir'], True)
            records.event(payload['session_id'],'job.result',{'job_id':path.name,'result_path':str(path/'result.json'),'ok':result.get('ok')})
    except BaseException as exc:
        publish(path/'result.json',{'ok':False,'error':type(exc).__name__+': '+str(exc)})
        publish(path/'status.json',dict(status,ok=False,status='failed',finished=time.time()))


if __name__=='__main__':
    worker(sys.argv[1])
