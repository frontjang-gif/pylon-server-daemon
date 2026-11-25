"""Call the VM downloader through the existing daemon capability channel."""
import json
import urllib.error
import urllib.request

CAPABILITY = 'cap:linux.download'


def request(method, path, data=None):
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request('http://127.0.0.1:8080' + path, data=payload, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return json.load(exc)


def submit(args, ctx):
    return request('POST', '/download', args)


def status(args, ctx):
    job_id = str(args.get('job_id', ''))
    if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('Invalid download job ID')
    return request('GET', '/jobs/' + job_id)


METHODS = {'submit': submit, 'status': status}
