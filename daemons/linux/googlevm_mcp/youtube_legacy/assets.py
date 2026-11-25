"""YouTube thumbnail capture."""
import base64
import json
import time
from typing import Any
from .browser import CDP, _open_target, _close_target

def youtube_thumbnail(video_id:str,url:str,port:int=9223)->dict[str,Any]:
    t=_open_target(port,f"https://www.youtube.com/watch?v={video_id}"); c=CDP(t["webSocketDebuggerUrl"])
    try:
        deadline=time.time()+10
        while True:
            try:
                if c.eval("typeof fetch==='function' && !!document"):
                    break
            except Exception as exc:
                if "Cannot find default execution context" not in str(exc):
                    raise
            if time.time()>=deadline:
                raise RuntimeError(f"YouTube thumbnail execution context did not initialize for {video_id}")
            time.sleep(.25)

        candidates=[
            url,
            f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        ]
        seen=set()
        last_status=None
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            expr=f"""(async()=>{{
              const r=await fetch({json.dumps(candidate)});
              if(!r.ok) return JSON.stringify({{ok:false,status:r.status}});
              const b=new Uint8Array(await r.arrayBuffer());
              let s='';
              for(let i=0;i<b.length;i+=0x8000) s+=String.fromCharCode(...b.subarray(i,i+0x8000));
              return JSON.stringify({{
                ok:true,
                status:r.status,
                contentType:r.headers.get('content-type')||'',
                finalUrl:r.url||{json.dumps(candidate)},
                base64:btoa(s)
              }});
            }})()"""
            result=json.loads(c.eval(expr) or "{}")
            last_status=result.get("status")
            if result.get("ok") and result.get("base64"):
                return {
                    "bytes":base64.b64decode(result["base64"]),
                    "contentType":result.get("contentType") or "",
                    "finalUrl":result.get("finalUrl") or candidate,
                }
        raise RuntimeError(f"YouTube thumbnail fetch failed for {video_id}: HTTP {last_status}")
    finally:
        c.close()
        if t.get("_openedByPkb"):
            _close_target(port,str(t.get("id") or ""))


