from __future__ import annotations
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any
import websocket

def _journal_navigation(url, action, target_id=''):
    import logging
    logging.getLogger('youtube.browser').info('%s %s target=%s',action,url,target_id)

class CDP:
    def __init__(self, ws_url: str):
        self.ws=websocket.create_connection(ws_url,timeout=30,suppress_origin=True)
        self._id=0
        self._events=[]

    def close(self):
        self.ws.close()

    def call(self,method:str,params:dict[str,Any]|None=None):
        self._id+=1; i=self._id
        self.ws.send(json.dumps({"id":i,"method":method,"params":params or {}}))
        while True:
            msg=json.loads(self.ws.recv())
            if msg.get("id")==i:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result",{})
            if msg.get("method"):
                self._events.append(msg)

    def recv_event(self,method:str,timeout:float):
        deadline=time.time()+timeout
        while True:
            for idx,msg in enumerate(self._events):
                if msg.get("method")==method:
                    return self._events.pop(idx)
            remaining=deadline-time.time()
            if remaining<=0:
                return None
            self.ws.settimeout(remaining)
            try:
                msg=json.loads(self.ws.recv())
            except Exception:
                return None
            if msg.get("method")==method:
                return msg
            if msg.get("method"):
                self._events.append(msg)

    def eval(self,expression:str):
        result=self.call("Runtime.evaluate",{
            "expression":expression,"returnByValue":True,"awaitPromise":True
        })
        return result.get("result",{}).get("value")


def target(port:int,contains:str)->dict[str,Any]:
    pages=json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json",timeout=10))
    return next(p for p in pages if p.get("type")=="page" and contains in str(p.get("url") or ""))


def _open_target(port:int,url:str)->dict[str,Any]:
    endpoint=f"http://127.0.0.1:{port}/json/new?"+urllib.parse.quote(url,safe=":/?=&")
    req=urllib.request.Request(endpoint,method="PUT")
    t=json.load(urllib.request.urlopen(req,timeout=10))
    t["_openedByPkb"]=True
    if not t.get("webSocketDebuggerUrl"):
        raise RuntimeError(f"CDP page did not open: {url}")
    time.sleep(1)
    _journal_navigation(url,"open",str(t.get("id") or ""))
    return t


def target_or_open(port:int,contains:str,url:str)->dict[str,Any]:
    try:
        t=target(port,contains); t["_openedByPkb"]=False; _journal_navigation(str(t.get("url") or url),"reuse",str(t.get("id") or "")); return t
    except Exception:
        return _open_target(port,url)



def _target_exists(port:int,target_id:str)->bool:
    try:
        pages=json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json",timeout=5))
        return any(str(p.get("id") or "")==target_id for p in pages)
    except Exception:
        return False


def _close_target(port:int,target_id:str)->None:
    if not target_id: return
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target_id}",timeout=5).read()
    except Exception:
        pass

    # /json/close is asynchronous. Do not return until the PKB-owned tab has
    # actually disappeared from the target list.
    for _ in range(20):
        if not _target_exists(port,target_id):
            return
        time.sleep(.1)

    # Fallback to the browser-level CDP command if the HTTP endpoint did not
    # finish closing the target.
    try:
        version=json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",timeout=5))
        ws_url=str(version.get("webSocketDebuggerUrl") or "")
        if ws_url:
            browser=CDP(ws_url)
            try:
                browser.call("Target.closeTarget",{"targetId":target_id})
            finally:
                browser.close()
    except Exception:
        pass

    for _ in range(30):
        if not _target_exists(port,target_id):
            return
        time.sleep(.1)



__all__=["CDP","target","_open_target","target_or_open","_target_exists","_close_target"]
