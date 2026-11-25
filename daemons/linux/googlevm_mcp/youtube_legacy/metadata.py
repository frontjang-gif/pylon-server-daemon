from __future__ import annotations
import json
import time
import urllib.request
from typing import Any
from .browser import CDP, _open_target, _close_target, target_or_open

def _text_language(*values:str)->str:
    text=" ".join(str(v or "") for v in values)
    ko=sum("가"<=ch<="힣" for ch in text)
    latin=sum(("a"<=ch.lower()<="z") for ch in text)
    if ko >= 3 and ko >= latin * 0.15:
        return "ko"
    if latin >= 3:
        return "en"
    return "und"


def youtube_metadata(video_id:str,port:int=9223,wait_seconds:float=15.0)->dict[str,Any]:
    # Request Korean localization explicitly. Without hl/gl, YouTube can
    # return a translated description based on the CDP profile locale.
    url=f"https://www.youtube.com/watch?v={video_id}&hl=ko&gl=KR"
    t=target_or_open(port,video_id,url); c=CDP(t["webSocketDebuggerUrl"])
    try:
        deadline=time.time()+wait_seconds
        player={}
        while time.time()<deadline:
            player=json.loads(c.eval("JSON.stringify(window.ytInitialPlayerResponse||{})") or "{}")
            d=player.get("videoDetails") or {}
            if d.get("title") and d.get("channelId"):
                break
            play=player.get("playabilityStatus") or {}
            if play.get("status")=="ERROR":
                reason=str(play.get("reason") or "")
                detail=""
                try:
                    detail=((((play.get("errorScreen") or {}).get("playerInterstitialRenderer") or {}).get("content") or {}).get("interstitialViewModel") or {}).get("description",{}).get("content","")
                except Exception:
                    detail=""
                from .capture import YouTubeUnavailableError
                raise YouTubeUnavailableError(f"{video_id}: {detail or reason or 'unavailable'}")
            time.sleep(.5)
        else:
            raise RuntimeError(f"YouTube metadata did not initialize for {video_id}")

        d=player.get("videoDetails") or {}
        m=((player.get("microformat") or {}).get("playerMicroformatRenderer") or {})
        thumbs=((d.get("thumbnail") or {}).get("thumbnails") or [])
        return {
            "title":d.get("title") or "",
            "description":d.get("shortDescription") or "",
            "descriptionLanguage":_text_language(d.get("title"),d.get("author"),d.get("shortDescription")),
            "descriptionLanguageBasis":"title+channel+description script detection",
            "channel":d.get("author") or "",
            "channelId":d.get("channelId") or "",
            "duration":int(d.get("lengthSeconds") or 0) or None,
            "viewCount":int(d.get("viewCount") or 0) or None,
            "publishedDate":str(m.get("publishDate") or m.get("uploadDate") or "")[:10],
            "category":m.get("category") or "",
            "keywords":d.get("keywords") or [],
            "image":thumbs[-1].get("url") if thumbs else "",
        }
    finally:
        c.close()
        if t.get("_openedByPkb"):
            _close_target(port,str(t.get("id") or ""))
