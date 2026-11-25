"""YouTube capture on the VM, with durable, component-aware bundles."""
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import Request, urlopen
import uuid


def video_id(value):
    value=str(value).strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{11}',value): return value
    parsed=urlsplit(value)
    host=(parsed.hostname or '').lower()
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password: raise ValueError('Supply a YouTube video URL')
    parts=parsed.path.strip('/').split('/')
    if host in ('youtu.be','www.youtu.be'): result=parts[0]
    elif host in ('youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtube-nocookie.com','www.youtube-nocookie.com'):
        result=parse_qs(parsed.query).get('v',[''])[0] if parsed.path=='/watch' else parts[1] if len(parts)>1 and parts[0] in ('shorts','embed','live') else ''
    else: result=''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',result): raise ValueError('Supply a single YouTube video URL, not a channel or playlist')
    return result


def now(): return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)
    finally: temporary.unlink(missing_ok=True)


def request(url):
    return urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=30)


def render(bundle):
    meta=bundle.get('metadata',{}); lines=['# '+str(meta.get('title') or bundle['video_id']),'',bundle['url'],'',
        '- Channel: '+str(meta.get('channel') or ''),'- Captured: '+bundle.get('updated',''),'- Status: '+bundle.get('state',''),'',str(meta.get('description') or '')]
    for track in bundle.get('captions',[]): lines+=['','## Transcript: '+track.get('language','und')+(' (automatic)' if track.get('automatic') else ''),'',track.get('text','')]
    if bundle.get('comments'): lines+=['','## Comments']+[str(item.get('author') or '')+': '+str(item.get('text') or '') for item in bundle['comments']]
    if bundle.get('warnings'):lines+=['','## Capture notes']+['- '+w for w in bundle['warnings']]
    return '\n'.join(lines)+'\n'


def list_captures(data_root):
    root=Path(data_root)/'youtube';items=[]
    if root.exists():
        for folder in root.iterdir():
            if not folder.is_dir() or not re.fullmatch(r'[A-Za-z0-9_-]{11}',folder.name):continue
            path=folder/'capture.json'
            if not path.exists():continue
            try:bundle=json.loads(path.read_text(encoding='utf-8'))
            except (ValueError,OSError):continue
            items.append(summary(bundle))
    return {'ok':True,'items':sorted(items,key=lambda item:item['updated'],reverse=True)}


def summary(bundle):
    meta=bundle.get('metadata',{})
    return {'video_id':bundle['video_id'],'title':meta.get('title') or bundle['video_id'],'channel':meta.get('channel',''),
        'url':bundle['url'],'updated':bundle.get('updated',''),'state':bundle.get('state','unknown'),
        'caption_tracks':len(bundle.get('captions',[])),'comment_count':len(bundle.get('comments',[])),
        'warnings':bundle.get('warnings',[]),'path':'data/youtube/'+bundle['video_id']+'/capture.json',
        'sha256':hashlib.sha256(json.dumps(bundle,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}


def get_capture(data_root, identifier):
    vid=video_id(identifier)
    return json.loads((Path(data_root)/'youtube'/vid/'capture.json').read_text(encoding='utf-8'))


def capture(url, data_root, backend='auto', max_comments=100):
    vid=video_id(url); canonical='https://www.youtube.com/watch?v='+vid
    if backend not in ('auto','http','browser'):raise ValueError('backend must be auto, http or browser')
    if max_comments<0:raise ValueError('max_comments must be nonnegative; zero means all')
    directory=Path(data_root)/'youtube'/vid;directory.mkdir(parents=True,exist_ok=True)
    # Serialize only captures of the same video across independent VM jobs.
    with (directory/'.capture.lock').open('a') as lock:
        if __import__('os').name!='nt':
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX)
        previous=get_capture(data_root,vid) if (directory/'capture.json').exists() else {}
        bundle={'version':1,'video_id':vid,'url':canonical,'updated':now(),'state':'running','metadata':{},'captions':[], 'comments':[], 'warnings':[], 'components':{},'engine':backend,'options':{'max_comments':max_comments}}
        def save():
            bundle['updated']=now();atomic_json(directory/'capture.json',bundle)
        save()
        try:
            with request('https://www.youtube.com/oembed?'+urlencode({'url':canonical,'format':'json'})) as response: meta=json.load(response)
            bundle['metadata']={'title':meta.get('title',''),'channel':meta.get('author_name',''),'channel_url':meta.get('author_url',''),'image':meta.get('thumbnail_url','')}
            bundle['components']['metadata']='oembed';save()
        except Exception as exc:bundle['warnings'].append('Metadata: '+str(exc))
        if backend in ('auto','http'):
            try: _http(bundle,max_comments)
            except Exception as exc:bundle['warnings'].append('HTTP extraction: '+str(exc))
            save()
        if backend=='browser' or (backend=='auto' and not bundle.get('captions')):
            try:_browser(bundle)
            except Exception as exc:bundle['warnings'].append('Browser extraction: '+str(exc))
            save()
        # Never erase a previously captured component because a refresh is blocked.
        bundle['metadata']={**previous.get('metadata',{}),**{k:v for k,v in bundle['metadata'].items() if v not in (None,'')}}
        def merge(component,key):
            values={key(item):item for item in previous.get(component,[])}
            values.update({key(item):item for item in bundle.get(component,[])})
            bundle[component]=list(values.values())
        merge('captions',lambda item:(item.get('language'),bool(item.get('automatic'))))
        merge('comments',lambda item:item.get('id') or item.get('commentId') or json.dumps(item,sort_keys=True,ensure_ascii=False))
        bundle['state']='partial' if bundle['warnings'] else 'complete'
        if not bundle['metadata'].get('title'):bundle['state']='failed'
        if bundle['captions']:bundle['components']['captions']='captured'
        else:bundle['components'].setdefault('captions','unavailable')
        if bundle['comments']:bundle['components']['comments']='captured'
        else:bundle['components'].setdefault('comments','unavailable')
        image=bundle['metadata'].get('image')
        if image:
            try:
                with request(image) as response: content=response.read()
                temporary=directory/('thumbnail.'+uuid.uuid4().hex+'.tmp');temporary.write_bytes(content);temporary.replace(directory/'thumbnail.jpg')
                bundle['thumbnail_file']='thumbnail.jpg'
            except Exception as exc:bundle['warnings'].append('Thumbnail: '+str(exc));bundle['state']='partial'
        save()
        text=directory/'index.md';temporary=text.with_suffix('.'+uuid.uuid4().hex+'.tmp');temporary.write_text(render(bundle),encoding='utf-8');temporary.replace(text)
        return dict(summary(bundle),ok=bundle['state']!='failed',saved_to=str(directory),components=bundle['components'])


def _http(bundle,max_comments):
    from yt_dlp import YoutubeDL
    class Logger:
        def debug(self,message):pass
        def warning(self,message):bundle['warnings'].append(str(message))
        def error(self,message):pass
    options={'quiet':True,'no_warnings':True,'logger':Logger(),'skip_download':True,'format':'best','noplaylist':True,'ignore_no_formats_error':True,
        'socket_timeout':20,'retries':1,'extractor_retries':1,'getcomments':True,
        'extractor_args':{'youtube':{'max_comments':[str(max_comments) if max_comments else 'all']}}}
    import importlib.util
    spec=importlib.util.find_spec('playwright')
    node=Path(spec.origin).parent/'driver'/'node' if spec and spec.origin else None
    if node and node.exists():options['js_runtimes']={'node':{'path':str(node)}}
    with YoutubeDL(options) as ydl:info=ydl.extract_info(bundle['url'],download=False)
    if not info:raise RuntimeError('YouTube returned no video information')
    bundle['metadata'].update({key:value for key,value in {
        'title':info.get('title'),'description':info.get('description'),'channel':info.get('channel') or info.get('uploader'),
        'channelId':info.get('channel_id'),'duration':info.get('duration'),'viewCount':info.get('view_count'),
        'publishedDate':info.get('upload_date'),'image':info.get('thumbnail'),'commentCount':info.get('comment_count')}.items() if value is not None})
    bundle['comments']=[{'id':c.get('id'),'parent':c.get('parent'),'author':c.get('author'),'text':c.get('text'),'likes':c.get('like_count'),'timestamp':c.get('timestamp')} for c in info.get('comments',[])]
    bundle['components']['metadata']='yt-dlp';bundle['components']['comments']='captured' if bundle['comments'] else 'none_returned'
    for automatic,key in [(False,'subtitles'),(True,'automatic_captions')]:
        for language,formats in (info.get(key) or {}).items():
            if language.split('-')[0] not in ('ko','en') or language.endswith('-orig'):continue
            track=next((item for item in formats if item.get('ext')=='json3'),None)
            if not track or parse_qs(urlsplit(track['url']).query).get('tlang'):continue
            try:
                with request(track['url']) as response: raw=json.load(response)
                segments=[{'startMs':e.get('tStartMs',0),'endMs':e.get('tStartMs',0)+e.get('dDurationMs',0),
                           'text':''.join(s.get('utf8','') for s in e.get('segs',[])).strip()} for e in raw.get('events',[]) if e.get('segs')]
                segments=[s for s in segments if s['text']]
                if segments:bundle['captions'].append({'language':language,'automatic':automatic,'segments':segments,'text':'\n'.join(s['text'] for s in segments)})
            except Exception as exc:bundle['warnings'].append('Captions '+language+': '+str(exc))


def _browser(bundle):
    import socket
    from playwright.sync_api import sync_playwright
    from .youtube_legacy.capture import youtube_dynamic,youtube_metadata
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--remote-debugging-port='+str(port)])
        try:
            metadata=youtube_metadata(bundle['video_id'],port=port)
            bundle['metadata'].update(metadata)
            dynamic=youtube_dynamic(bundle['video_id'],port=port,max_comments=bundle.get('options',{}).get('max_comments',100))
            bundle['captions']=dynamic.get('captions') or bundle['captions'];bundle['comments']=dynamic.get('comments') or bundle['comments']
            bundle['components']['browser_comments_complete']=dynamic.get('commentsComplete',False)
            if not dynamic.get('commentsComplete'):bundle['warnings'].append('Browser comments are incomplete')
        finally:browser.close()
