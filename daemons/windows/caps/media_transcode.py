"""cap:media.transcode — bounded format conversion, never arbitrary commands."""
import base64
from pathlib import Path
import subprocess
import tempfile
import math
import bisect
import time

CAPABILITY='cap:media.transcode'
VARIANT='ffmpeg'
ATTRS={'formats':['webm','mp4']}


def convert(args,ctx):
    import imageio_ffmpeg
    source_format=args.get('source_format','webm');target_format=args.get('target_format','mp4')
    if source_format not in ('webm','mp4') or target_format not in ('webm','mp4'):return {'ok':False,'error':'Supported formats: webm, mp4'}
    try:
        raw=base64.b64decode(args.get('data_b64') or '',validate=True)
        if not raw or len(raw)>64*1024*1024:return {'ok':False,'error':'Input must be 1 byte to 64 MiB'}
        with tempfile.TemporaryDirectory(prefix='pylon-media-') as directory:
            source=Path(directory)/('input.'+source_format);output=Path(directory)/('output.'+target_format);source.write_bytes(raw)
            codec=['-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart'] if target_format=='mp4' else ['-c:v','libvpx-vp9','-b:v','2M']
            result=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-y','-i',str(source),'-an',*codec,str(output)],capture_output=True,timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:return {'ok':False,'error':'Media conversion failed: '+result.stderr.decode(errors='replace')[-500:]}
            encoded=output.read_bytes()
            if len(encoded)>64*1024*1024:return {'ok':False,'error':'Converted media exceeds 64 MiB'}
            return {'ok':True,'format':target_format,'data_b64':base64.b64encode(encoded).decode(),'bytes':len(encoded)}
    except Exception as exc:return {'ok':False,'error':str(exc)}


def render_route(args,ctx):
    """Encode captured source pixels with a moving marker and waypoint pauses."""
    import imageio_ffmpeg
    from PIL import Image,ImageDraw
    import io
    try:
        target=args.get('format','mp4');seconds=float(args.get('seconds',12));fps=int(args.get('fps',30));points=args['scene']['points']
        if target not in ('webm','mp4') or not .2<=seconds<=120 or not 1<=fps<=60:raise ValueError('Invalid animation format, duration or frame rate')
        if not 2<=len(points)<=100000 or any(len(p)!=2 or any(not math.isfinite(float(v)) for v in p) for p in points):raise ValueError('Invalid source pixel geometry')
        raw=base64.b64decode(args['background_b64'],validate=True)
        if len(raw)>16*1024*1024:raise ValueError('Background exceeds 16 MiB')
        with Image.open(io.BytesIO(raw)) as original:
            if original.width*original.height>9_000_000:raise ValueError('Background too large')
            background=original.convert('RGB')
        width,height=background.size;width-=width%2;height-=height%2;background=background.crop((0,0,width,height))
        if min(width,height)<2:raise ValueError('Invalid background dimensions')
        lengths=[0.]
        for a,b in zip(points,points[1:]):lengths.append(lengths[-1]+math.dist(a,b))
        total=lengths[-1]
        if not total:raise ValueError('Source path has no length')
        pauses=sorted([{'fraction':max(0,min(1,float(p['fraction']))),'seconds':max(0,min(120,float(p.get('seconds',1.5))))} for p in args['scene'].get('stops',[])],key=lambda p:p['fraction'])
        pause_total=sum(p['seconds'] for p in pauses);scale=min(1,seconds*.75/pause_total) if pause_total else 1;travel=seconds-pause_total*scale
        def fraction(t):
            used=previous=0
            for p in pauses:
                moving=(p['fraction']-previous)*travel
                if t<used+moving:return previous+(t-used)/travel
                used+=moving
                if t<used+p['seconds']*scale:return p['fraction']
                used+=p['seconds']*scale;previous=p['fraction']
            return min(1,previous+(t-used)/travel)
        with tempfile.TemporaryDirectory(prefix='pylon-route-') as directory:
            output=Path(directory)/('route.'+target);codec=['-c:v','libx264','-preset','fast','-pix_fmt','yuv420p','-movflags','+faststart'] if target=='mp4' else ['-c:v','libvpx-vp9','-deadline','realtime','-cpu-used','5','-b:v','2M']
            command=[imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-y','-f','rawvideo','-pix_fmt','rgb24','-s',f'{width}x{height}','-r',str(fps),'-i','pipe:0','-an',*codec,str(output)]
            with (Path(directory)/'error.log').open('wb') as errors:
                process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=errors,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));started=time.monotonic()
                try:
                    count=max(2,math.ceil(seconds*fps))
                    for i in range(count):
                        if time.monotonic()-started>540:raise TimeoutError('Route rendering exceeded its time budget')
                        target_length=fraction(i/(count-1)*seconds)*total;index=min(len(points)-1,max(1,bisect.bisect_left(lengths,target_length)));part=(target_length-lengths[index-1])/(lengths[index]-lengths[index-1] or 1);a,b=points[index-1],points[index];x=a[0]+(b[0]-a[0])*part;y=a[1]+(b[1]-a[1])*part
                        image=background.copy();ImageDraw.Draw(image).ellipse((x-9,y-9,x+9,y+9),fill='#ffc000',outline='#222222',width=2);process.stdin.write(image.tobytes())
                    process.stdin.close();process.wait(timeout=max(1,600-(time.monotonic()-started)))
                    if process.returncode:raise ValueError('Route encoder failed')
                finally:
                    if process.poll() is None:process.kill();process.wait()
                    if not process.stdin.closed:process.stdin.close()
            raw=output.read_bytes()
            if not raw or len(raw)>64*1024*1024:raise ValueError('Rendered video is empty or exceeds 64 MiB')
            return {'ok':True,'format':target,'data_b64':base64.b64encode(raw).decode(),'bytes':len(raw),'frames':count,'seconds':seconds}
    except Exception as exc:return {'ok':False,'error':str(exc)}


METHODS={'convert':convert,'render_route':render_route}
