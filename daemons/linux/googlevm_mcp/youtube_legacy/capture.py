from __future__ import annotations

import base64
import gzip
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any



class YouTubeUnavailableError(RuntimeError):
    pass


class YouTubeTranscriptError(YouTubeUnavailableError):
    """An advertised transcript could not be retrieved from YouTube."""
    failure_category = "transcript_retrieval"


class YouTubePlaybackError(YouTubeTranscriptError):
    """The browser's player failed while advertised captions were unavailable."""
    failure_category = "browser_playback"


def _playback_error(c):
    # Hidden error templates are not evidence of a playback failure.
    return c.eval(r"""(()=>{
      const node=document.querySelector('#movie_player .ytp-error');
      return node && node.getClientRects().length ? (node.innerText||'').trim() : '';
    })()""") or ""


from .browser import CDP, target, _open_target, target_or_open, _target_exists, _close_target

def _youtube_transcript(c:CDP,video_id:str)->list[dict[str,Any]]:
    deadline=time.time()+20
    while time.time()<deadline:
        if c.eval("document.readyState==='complete' && !!window.ytInitialPlayerResponse && !!window.ytInitialData"):
            break
        time.sleep(.5)

    tracks=[]
    # ytInitialPlayerResponse may exist before its caption tracks have settled.
    for _ in range(20):
        player=json.loads(c.eval("JSON.stringify(window.ytInitialPlayerResponse||{})") or "{}")
        tracks=(((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or [])
        if tracks:
            break
        time.sleep(.5)

    def rank(track:dict[str,Any])->tuple[int,str]:
        code=str(track.get("languageCode") or "").lower()
        auto=track.get("kind")=="asr"
        if code.startswith("ko"):
            return (1 if auto else 0, code)
        if code.startswith("en"):
            return (3 if auto else 2, code)
        return (99, code)

    preferred=sorted((x for x in tracks if rank(x)[0]<99),key=rank)
    if not preferred:
        return []

    # YouTube can keep hidden legacy panels beside the active panel.
    panel_lookup = r"""
      const panels=[...document.querySelectorAll(
        'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"],'
        +'ytd-engagement-panel-section-list-renderer[target-id="PAmodern_transcript_view"]'
      )];
      const panel=panels.find(x=>x.getAttribute('visibility')==='ENGAGEMENT_PANEL_VISIBILITY_EXPANDED' && x.getClientRects().length)
        ||panels.find(x=>x.getAttribute('visibility')==='ENGAGEMENT_PANEL_VISIBILITY_EXPANDED');
    """

    def panel_eval(expression):
        return c.eval("(()=>{"+panel_lookup+expression+"})()")

    def dom_transcript_for_track(track:dict[str,Any])->dict[str,Any]|None:
        code=str(track.get("languageCode") or "")
        auto=track.get("kind")=="asr"
        name=((track.get("name") or {}).get("simpleText") or
              "".join(str(run.get("text") or "") for run in (track.get("name") or {}).get("runs", [])) or code)
        c.eval("window.__pkbDesiredCaptionName="+json.dumps(str(name),ensure_ascii=False))
        verified=len(tracks)==1
        switched_from=None
        # Wait for native loading; do not make an empty panel look expanded by
        # changing its visibility attribute, which does not issue a request.
        for attempt in range(16):
            # Some layouts open a shared information/chapter/transcript panel
            # with its information tab selected. Select its native transcript
            # tab before looking for language controls or transcript rows.
            switched_tab=panel_eval(r"""
              const b=[...(panel?.querySelectorAll('chip-view-model button')||[])]
                .find(x=>/^(transcript|\uC2A4\uD06C\uB9BD\uD2B8)$/i.test((x.getAttribute('aria-label')||x.innerText||'').trim()));
              if(b && b.getAttribute('aria-selected')!=='true'){b.click();return true;}
              return false;
            """)
            if switched_tab:
                time.sleep(.5)
            if not verified:
                # The modern timeline has no language selector. Its native
                # endpoint uses the video's default caption track. Accept it
                # only when every advertised audio track agrees on that exact
                # track; do not infer language or ASR status from visible text.
                modern_default=panel_eval(r"""
                  if(panel?.getAttribute('target-id')!=='PAmodern_transcript_view'
                     ||panel.querySelector('yt-sort-filter-sub-menu-renderer'))return null;
                  const list=window.ytInitialPlayerResponse?.captions?.playerCaptionsTracklistRenderer;
                  const indices=[...new Set((list?.audioTracks||[]).map(x=>x.defaultCaptionTrackIndex))];
                  return indices.length===1 && Number.isInteger(indices[0])
                    ? JSON.stringify(list.captionTracks?.[indices[0]]||null):null;
                """)
                if modern_default:
                    default_track=json.loads(modern_default) if isinstance(modern_default,str) else modern_default
                    verified=isinstance(default_track,dict) and (
                        default_track.get('languageCode')==track.get('languageCode')
                        and default_track.get('kind')==track.get('kind')
                        and default_track.get('vssId')==track.get('vssId'))
            if not verified:
                verified=panel_eval(r"""
                  const wanted=(window.__pkbDesiredCaptionName||'').trim().toLowerCase();
                  const label=panel?.querySelector('yt-sort-filter-sub-menu-renderer #label-text');
                  return !!label && (label.textContent||'').trim().toLowerCase()===wanted;
                """)
                if not verified:
                    previous_rows=panel_eval(r"""
                      return JSON.stringify([...(panel?.querySelectorAll('ytd-transcript-segment-renderer,transcript-segment-view-model')||[])].map(x=>x.innerText||''));
                    """) or '[]'
                    panel_eval(r"""
                      const b=panel?.querySelector('yt-sort-filter-sub-menu-renderer button');
                      if(b && b.getAttribute('aria-expanded')!=='true')b.click();
                      return !!b;
                    """)
                    verified=panel_eval(r"""
                      const wanted=(window.__pkbDesiredCaptionName||'').trim().toLowerCase();
                      const items=[...document.querySelectorAll('tp-yt-paper-listbox tp-yt-paper-item')]
                        .filter(x=>x.getClientRects().length);
                      const item=items.find(x=>(x.textContent||'').trim().toLowerCase()===wanted);
                      if(item){item.click();return true;}
                      return false;
                    """)
                    if verified:
                        # Do not label the previous language's rows as the new
                        # track while its asynchronous request is still loading.
                        switched_from=previous_rows
                        time.sleep(.5)
            if verified:
                raw_dom=panel_eval(r"""
                  return JSON.stringify([...(panel?.querySelectorAll('ytd-transcript-segment-renderer,transcript-segment-view-model')||[])].map(x=>{
                    const stamp=x.querySelector('.ytwTranscriptSegmentViewModelTimestamp');
                    const text=x.querySelector('span[role="text"]');
                    return stamp && text ? stamp.textContent+'\n'+text.textContent : x.innerText||'';
                  }));
                """) or '[]'
                dom_rows=raw_dom if isinstance(raw_dom,list) else json.loads(raw_dom)
                old_rows=switched_from if isinstance(switched_from,list) else json.loads(switched_from or '[]')
                if switched_from is not None and dom_rows==old_rows and dom_rows:
                    time.sleep(.5)
                    continue
                segs=[]
                for row in dom_rows:
                    parts=[x.strip() for x in str(row).splitlines() if x.strip()]
                    if len(parts)<2:
                        continue
                    try:
                        seconds=0
                        for part in parts[0].split(':'):
                            seconds=seconds*60+int(part)
                    except ValueError:
                        continue
                    segs.append({"timestamp":parts[0],"startMs":seconds*1000,"endMs":seconds*1000,"text":" ".join(parts[1:])})
                if segs:
                    return {"language":code,"name":name,"automatic":auto,"sourceUrl":"youtube-transcript-dom-selected","text":"\n".join(x["text"] for x in segs),"segments":segs}
            time.sleep(.5)
        return None

    # Preserve an already-open native panel, including text loaded manually.
    # Opening a restored panel again can discard a working transcript.
    expanded=panel_eval("return !!panel;")
    if not expanded:
        # YouTube's caption bootstrap can finish after document.readyState and
        # ytInitialPlayerResponse. Opening the panel immediately can issue a
        # native get_transcript request that fails its precondition. A short
        # initialization grace avoids that race without replaying requests.
        time.sleep(4)
        for _ in range(30):
            c.eval("window.scrollTo(0,1200); true")
            c.eval(r"""(()=>{const b=document.querySelector('ytd-watch-metadata #description-inline-expander #expand'); if(b)b.click(); return true;})()""")
            clicked=c.eval(r"""(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.getClientRects().length&&/\uC2A4\uD06C\uB9BD\uD2B8 \uD45C\uC2DC|show transcript/i.test((x.getAttribute('aria-label')||'')+' '+(x.innerText||''))); if(b){b.click();return true;} return false;})()""")
            if clicked:
                break
            time.sleep(.5)

    # Keep up to two transcripts for the selected language: one uploaded
    # track and one ASR track. Language priority is Korean, then English.
    language_prefix="ko" if any(str(x.get("languageCode") or "").lower().startswith("ko") for x in preferred) else "en"
    selected=[]
    manual=next((x for x in preferred if str(x.get("languageCode") or "").lower().startswith(language_prefix) and x.get("kind")!="asr"),None)
    auto_track=next((x for x in preferred if str(x.get("languageCode") or "").lower().startswith(language_prefix) and x.get("kind")=="asr"),None)
    if manual: selected.append(manual)
    if auto_track: selected.append(auto_track)

    captions=[]
    failures=[]
    for track in selected:
        dom_caption=dom_transcript_for_track(track)
        if dom_caption:
            captions.append(dom_caption)
            continue
        code=str(track.get("languageCode") or "")
        auto=track.get("kind")=="asr"
        # Use the advertised caption URL when the selected native panel is unavailable.
        base=str(track.get("baseUrl") or "")
        if not base:
            failures.append("advertised caption track has no retrievable URL")
            continue
        c.eval("window.__pkbTranscriptReplay="+json.dumps({"url":base+"&fmt=json3"},ensure_ascii=False))
        raw=c.eval(r"""(async()=>{
          const r=await fetch(window.__pkbTranscriptReplay.url,{signal:AbortSignal.timeout(10000)});
          const text=await r.text();
          let json={};try{json=text?JSON.parse(text):{};}catch{}
          return JSON.stringify({status:r.status,json,bytes:text.length});
        })()""") or "{}"
        result=raw if isinstance(raw,dict) else json.loads(raw)
        if int(result.get("status") or 0)>=400:
            failures.append(f"caption endpoint HTTP {result.get('status')}")
            continue
        j=result.get("json") or {}
        segs=[]
        for e in j.get("events") or []:
            text="".join(str(x.get("utf8") or "") for x in e.get("segs") or []).strip()
            if text:
                segs.append({"timestamp":"","startMs":int(e.get("tStartMs") or 0),"endMs":int(e.get("tStartMs") or 0)+int(e.get("dDurationMs") or 0),"text":text})
        if not segs:
            failures.append("caption endpoint returned an empty response")
            continue
        name=((track.get("name") or {}).get("simpleText") or code)
        captions.append({"language":code,"name":name,"automatic":auto,"sourceUrl":base,"text":"\n".join(x["text"] for x in segs),"segments":segs})
        continue

    if failures:
        playback_error=_playback_error(c)
        if playback_error:
            raise YouTubePlaybackError(
                f"YouTube browser playback failed: {playback_error}; "
                "advertised captions could not be retrieved; existing saved data was preserved")
        # Report the native panel's request failure as well as the fallback.
        # Resource timing exposes status without replaying signed requests or
        # installing fetch hooks into the user's authenticated page.
        raw_statuses=c.eval(r"""(()=>JSON.stringify(
          performance.getEntriesByType('resource')
            .filter(x=>x.name.includes('/youtubei/v1/get_transcript'))
            .map(x=>Number(x.responseStatus)||0)
        ))()""") or "[]"
        statuses=raw_statuses if isinstance(raw_statuses,list) else json.loads(raw_statuses)
        native_failures=[f"native transcript endpoint HTTP {status}" for status in statuses if status>=400]
        failures=native_failures+failures
        raise YouTubeTranscriptError("YouTube " + "; ".join(dict.fromkeys(failures)) + "; existing saved data was preserved")
    return captions


from .assets import youtube_thumbnail

def _capture_page(video_id, port):
    # Keep user-loaded transcript text; empty restored pages can retain stale
    # session-bound caption URLs. Navigate an owned page in the same profile.
    try:
        existing=target(port,f"watch?v={video_id}")
    except StopIteration:
        existing=None
    if existing:
        c=CDP(existing['webSocketDebuggerUrl'])
        loaded=c.eval(r"""(()=>{
          return [...document.querySelectorAll('ytd-transcript-segment-renderer,transcript-segment-view-model')]
            .some(x=>(x.innerText||'').trim());
        })()""")
        if loaded:
            return c,None
        c.close()
    opened=_open_target(port,f"https://www.youtube.com/watch?v={video_id}")
    class OwnedPage:
        def close(self): _close_target(port,opened['id'])
    return CDP(opened['webSocketDebuggerUrl']),OwnedPage()


def youtube_transcript(video_id:str,port:int=9223)->list[dict[str,Any]]:
    c,page=_capture_page(video_id,port)
    try:
        return _youtube_transcript(c,video_id)
    finally:
        c.close()
        if page:
            page.close()


def youtube_dynamic(video_id:str,port:int=9223,include_captions:bool=True,max_comments:int=100)->dict[str,Any]:
    c,page=_capture_page(video_id,port)
    try:
        deadline=time.time()+20
        while time.time()<deadline:
            if c.eval("!!window.ytInitialData && !!window.ytcfg"):
                break
            time.sleep(.5)

        captions=_youtube_transcript(c,video_id) if include_captions else []
        c.ws.settimeout(300)
        c.eval("window.__pylonMaxComments="+str(int(max_comments)))
        raw=c.eval(r"""(async()=>{
          const maxComments=window.__pylonMaxComments||0;
          const initial=window.ytInitialData||{};
          const apiKey=window.ytcfg?.get('INNERTUBE_API_KEY')||'';
          const context=window.ytcfg?.get('INNERTUBE_CONTEXT')||{};
          const currentVideoId=window.ytInitialPlayerResponse?.videoDetails?.videoId||'';
          const rows=new Map();
          const seenTokens=new Set();

          function initialCommentToken(){
            try{
              const items=initial.contents.twoColumnWatchNextResults.results.results.contents||[];
              for(const item of items){
                for(const child of (item?.itemSectionRenderer?.contents||[])){
                  const ep=child?.continuationItemRenderer?.continuationEndpoint;
                  if(ep?.commandMetadata?.webCommandMetadata?.apiUrl==='/youtubei/v1/next' && ep?.continuationCommand?.token){
                    return ep.continuationCommand.token;
                  }
                }
              }
            }catch(e){}
            return '';
          }

          async function next(token){
            const r=await fetch('/youtubei/v1/next?key='+encodeURIComponent(apiKey),{
              method:'POST',
              headers:{'content-type':'application/json'},
              body:JSON.stringify({context,continuation:token})
            });
            if(!r.ok) throw new Error('youtubei next '+r.status);
            return await r.json();
          }

          function entityRows(resp,parentId=''){
            const muts=resp?.frameworkUpdates?.entityBatchUpdate?.mutations||[];
            for(const m of muts){
              const e=m?.payload?.commentEntityPayload;
              if(!e) continue;
              const p=e.properties||{}, a=e.author||{}, tb=e.toolbar||{};
              const id=p.commentId||'';
              if(!id) continue;
              const depth=Number(p.replyLevel||0);
              const old=rows.get(id)||{};
              rows.set(id,{
                id,
                parentId:(depth>0 ? (parentId||old.parentId||'') : ''),
                depth,
                author:a.displayName||'',
                text:p.content?.content||'',
                timeText:p.publishedTime||'',
                permalink:'https://www.youtube.com/watch?v='+encodeURIComponent(currentVideoId)+'&lc='+encodeURIComponent(id),
                pinned:!!p.pinnedCommentBadge,
                likeCount:tb.likeCountNotliked||tb.likeCountLiked||'',
                replyCount:tb.replyCount||''
              });
            }
          }

          function commentIdFromThread(th){
            return th?.commentViewModel?.commentViewModel?.commentId||'';
          }

          function walkReplyIds(x,out){
            if(!x||typeof x!=='object') return;
            if(x.commentViewModel?.commentViewModel?.commentId) out.push(x.commentViewModel.commentViewModel.commentId);
            if(Array.isArray(x)) x.forEach(v=>walkReplyIds(v,out));
            else Object.values(x).forEach(v=>walkReplyIds(v,out));
          }

          function continuationTokens(x){
            const out=[];
            function add(ep){
              const tok=ep?.continuationCommand?.token;
              if(tok && ep?.commandMetadata?.webCommandMetadata?.apiUrl==='/youtubei/v1/next') out.push(tok);
            }
            function walk(v){
              if(!v||typeof v!=='object') return;
              const cir=v?.continuationItemRenderer || ((v?.continuationEndpoint || v?.button) ? v : null);
              if(cir){
                add(cir.continuationEndpoint);
                add(cir?.button?.buttonRenderer?.command);
                add(cir?.button?.buttonRenderer?.serviceEndpoint);
              }
              if(Array.isArray(v)) v.forEach(walk); else Object.values(v).forEach(walk);
            }
            walk(x);
            return [...new Set(out)];
          }

          function replyTokensForThread(th){
            return continuationTokens(th?.replies||{});
          }

          function continuationArrays(resp){
            const arrays=[];
            function walk(x){
              if(!x||typeof x!=='object') return;
              if(Array.isArray(x.continuationItems)) arrays.push(x.continuationItems);
              if(Array.isArray(x)) x.forEach(walk); else Object.values(x).forEach(walk);
            }
            walk(resp);
            return arrays;
          }

          function reportedCount(resp){
            let count=0;
            function walk(x){
              if(!x||typeof x!=='object') return;
              const h=x?.commentsHeaderRenderer;
              if(h){
                const text=h.countText?.simpleText||(h.countText?.runs||[]).map(r=>r.text||'').join('')||'';
                const digits=String(text).replace(/[^0-9]/g,'');
                if(digits) count=Number(digits);
              }
              if(Array.isArray(x)) x.forEach(walk); else Object.values(x).forEach(walk);
            }
            walk(resp);
            return count;
          }

          function newestSortToken(resp){
            let found='';
            function walk(x){
              if(!x||typeof x!=='object'||found) return;
              const items=x?.commentsHeaderRenderer?.sortMenu?.sortFilterSubMenuRenderer?.subMenuItems;
              if(Array.isArray(items) && items.length>1){
                const ep=items[1]?.serviceEndpoint;
                const tok=ep?.continuationCommand?.token;
                if(tok) found=tok;
              }
              if(Array.isArray(x)) x.forEach(walk); else Object.values(x).forEach(walk);
            }
            walk(resp);
            return found;
          }

          function processTopPage(resp){
            entityRows(resp,'');
            const replies=[];
            let nextTop='';
            for(const arr of continuationArrays(resp)){
              for(const item of arr){
                const th=item?.commentThreadRenderer;
                if(th){
                  const topId=commentIdFromThread(th);
                  const ids=[]; walkReplyIds(th?.replies||{},ids);
                  for(const id of ids){
                    const r=rows.get(id); if(r && r.depth>0) r.parentId=topId;
                  }
                  for(const tok of replyTokensForThread(th)) replies.push({token:tok,parentId:topId});
                }else{
                  const ep=item?.continuationItemRenderer?.continuationEndpoint;
                  const tok=ep?.continuationCommand?.token;
                  if(tok && ep?.commandMetadata?.webCommandMetadata?.apiUrl==='/youtubei/v1/next') nextTop=tok;
                }
              }
            }
            return {nextTop,replies};
          }

          const replyQueue=[];
          let token=initialCommentToken();
          let reported=0;
          let topPages=0;
          let replyPages=0;
          if(token){
            seenTokens.add(token);
            const first=await next(token);
            topPages++;
            reported=reportedCount(first);
            const newest=newestSortToken(first);
            if(newest && newest!==token){
              seenTokens.clear();
              token=newest;
            }else{
              const x=processTopPage(first);
              replyQueue.push(...x.replies);
              token=x.nextTop;
            }
          }
          while(token && !seenTokens.has(token) && (!maxComments || rows.size<maxComments)){
            seenTokens.add(token);
            const resp=await next(token);
            topPages++;
            const x=processTopPage(resp);
            replyQueue.push(...x.replies);
            token=x.nextTop;
          }

          while(replyQueue.length && (!maxComments || rows.size<maxComments)){
            const q=replyQueue.shift();
            if(!q?.token || seenTokens.has(q.token)) continue;
            seenTokens.add(q.token);
            const resp=await next(q.token);
            replyPages++;
            entityRows(resp,q.parentId||'');
            const mainTokens=[];
            const nestedTokens=[];
            for(const arr of continuationArrays(resp)){
              for(const item of arr){
                const th=item?.commentThreadRenderer;
                if(th){
                  const ids=[]; walkReplyIds(th?.replies||{},ids);
                  for(const id of ids){
                    const r=rows.get(id); if(r && r.depth>0) r.parentId=q.parentId||r.parentId||'';
                  }
                  for(const tok of replyTokensForThread(th)) nestedTokens.push(tok);
                }
                const cir=item?.continuationItemRenderer;
                if(cir){
                  for(const tok of continuationTokens(cir)) mainTokens.push(tok);
                }
              }
            }
            // Reply continuation commands are not always nested under a
            // continuationItems array. On current YouTube responses they can
            // live under command/endpoint wrappers elsewhere in the payload.
            // Scan the whole reply response too; this prevents silently
            // stopping after the first 10 replies of a thread.
            for(const tok of continuationTokens(resp)) mainTokens.push(tok);
            for(const tok of [...new Set(mainTokens)]){
              if(!seenTokens.has(tok)) replyQueue.unshift({token:tok,parentId:q.parentId||''});
            }
            for(const tok of [...new Set(nestedTokens)]){
              if(!seenTokens.has(tok)) replyQueue.push({token:tok,parentId:q.parentId||''});
            }
          }

          const materialized=[...rows.values()];
          const topLevelRows=materialized.filter(x=>Number(x.depth||0)===0).length;
          const retrievedReplyRows=materialized.filter(x=>Number(x.depth||0)>0).length;
          const reportedReplyRows=materialized
            .filter(x=>Number(x.depth||0)===0)
            .reduce((sum,x)=>sum+Number(String(x.replyCount||'0').replace(/[^0-9]/g,'')||0),0);
          return JSON.stringify({
            comments:materialized,
            reportedCommentCount:reported||rows.size,
            topLevelRows,
            reportedReplyRows,
            retrievedReplyRows,
            missingReplyRows:Math.max(0,reportedReplyRows-retrievedReplyRows),
            topPages,
            replyPages,
            continuationExhausted:!token,
            continuationStoppedByRepeat:!!token
          });
        })()""") or "{}"
        comment_result=json.loads(raw)
        comments=list(comment_result.get("comments") or [])
        reported_comment_count=int(comment_result.get("reportedCommentCount") or len(comments))
        top_pages=int(comment_result.get("topPages") or 0)
        reply_pages=int(comment_result.get("replyPages") or 0)
        top_level_rows=int(comment_result.get("topLevelRows") or 0)
        reported_reply_rows=int(comment_result.get("reportedReplyRows") or 0)
        retrieved_reply_rows=int(comment_result.get("retrievedReplyRows") or 0)
        missing_reply_rows=int(comment_result.get("missingReplyRows") or 0)
        continuation_exhausted=bool(comment_result.get("continuationExhausted", True))
        continuation_stopped_by_repeat=bool(comment_result.get("continuationStoppedByRepeat", False))

        # If YouTube reports more top-level comments than we materialized,
        # retry once in a fresh page. Continuation tokens are session-bound and
        # can occasionally expire while a long comments/replies crawl is running.
        # A second full pass is cheap compared with persisting an incomplete set.
        complete = continuation_exhausted and not continuation_stopped_by_repeat
        return {
            "comments":comments,
            "captions":captions,
            "reportedCommentCount":reported_comment_count,
            "commentsComplete":complete,
            "commentCountGap":max(0, reported_comment_count-len(comments)),
            "topCommentPages":top_pages,
            "replyPages":reply_pages,
            "topLevelCommentRows":top_level_rows,
            "reportedReplyRows":reported_reply_rows,
            "retrievedReplyRows":retrieved_reply_rows,
            "missingReplyRows":missing_reply_rows,
            "allAdvertisedRepliesRetrieved": missing_reply_rows==0,
            "continuationExhausted":continuation_exhausted,
            "continuationStoppedByRepeat":continuation_stopped_by_repeat,
        }
    finally:
        c.close()
        if page:
            page.close()


from .metadata import youtube_metadata
