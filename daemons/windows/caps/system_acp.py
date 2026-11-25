"""capability: cap:system.acp  (kiro-cli agent on this endpoint).

Endpoint-bound because it runs the LOCAL kiro-cli — the executor lives where the
binary + its session/auth live. The OS DAEMON owns execution; apps call this cap
without knowing where kiro runs (the "kiro plugin" in the llm app is a thin cap
CLIENT, not a process manager).

What moved here (from the llm app's kiro backend):
  * the persistent ``kiro-cli acp`` subprocess driver (JSON-RPC over stdio),
  * a warm DRIVER POOL for parallel one-shot prompts,
  * PINNED (sticky) drivers keyed by ``pool_id`` for concurrent stateful
    multi-turn conversations,
  * LIVE TOKEN STREAMING: the ``prompt`` method accepts an ``emit`` callback
    (the daemon's additive bidirectional-streaming hook) and pushes each ACP
    token chunk as a ``stream`` frame (event="delta") BEFORE the final result.

Methods (def fn(args, ctx[, emit]) -> dict):
  version()                      -> {ok, version}
  status()                       -> {ok, available, version, pool_size,
                                     pool_warm, pool_pinned}
  prompt(message, pool_id?, oneshot?, system?, reset?, timeout?, images?, emit?)
        -> {ok, content, pool_id?, pooled?}   (streams delta frames via emit)
        images: [{mimeType, data(base64)}] — appended as ACP image prompt blocks
        (kiro promptCapabilities.image); the vision/OCR input path.
  release(pool_id?)              -> {ok, released:[...]}   (free pinned driver(s))

Single-user today: ctx.user is informational (the local OS user runs kiro-cli).
"""
from __future__ import annotations

import json
import os
import queue as _queue
import shutil
import subprocess
import threading
import time

_INIT_TIMEOUT = 30
_PROMPT_TIMEOUT = 600


def _env_int(name, default, lo=0, hi=None):
    try:
        v = int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        v = default
    v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# Pool lifecycle budgets (IMPROVEMENTS #31/#32), all env-overridable:
#   * KIRO_POOL_LEASE_TIMEOUT  — max seconds to WAIT for a free pooled slot
#     (a wedged pool can't block a prompt indefinitely).
#   * KIRO_POOL_PROMPT_TIMEOUT — hard per-prompt budget for a POOLED prompt
#     (one-shot/pinned); 0 = fall back to _PROMPT_TIMEOUT.
#   * KIRO_POOL_IDLE_TTL       — evict a PINNED driver idle longer than this many
#     seconds (0 disables idle eviction). One-shot idle drivers are kept warm.
def _lease_timeout():
    return _env_int("KIRO_POOL_LEASE_TIMEOUT", 120, lo=1)


def _pool_prompt_timeout():
    return _env_int("KIRO_POOL_PROMPT_TIMEOUT", 0, lo=0)


def _idle_ttl():
    return _env_int("KIRO_POOL_IDLE_TTL", 900, lo=0)


def _kiro_bin():
    return (os.environ.get("KIRO_BIN") or os.environ.get("KIRO_CLI")
            or shutil.which("kiro") or shutil.which("kiro-cli") or "")


# --------------------------------------------------------------------------- #
# One kiro-cli ACP subprocess + session.
# --------------------------------------------------------------------------- #
class _Driver:
    def __init__(self):
        self.proc = None
        self.alive = False
        self.session = None
        self.id = 0
        self.lock = threading.Lock()
        self.pending = {}
        self.events = []
        self._prompt_lock = threading.Lock()
        self._system_sent = False

    def _nid(self):
        self.id += 1
        return self.id

    def _send(self, method, params):
        mid = self._nid()
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid,
                               "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        return mid

    def _reader(self):
        f = self.proc.stdout
        while self.alive:
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
            with self.lock:
                if "id" in msg and ("result" in msg or "error" in msg):
                    self.pending[msg["id"]] = msg
                else:
                    self.events.append(msg)
        self.alive = False

    def _drain(self):
        with self.lock:
            evs, self.events = self.events, []
        return evs

    @staticmethod
    def _extract(params):
        """Pull assistant text out of an ACP session/update event."""
        u = params.get("update") or params
        for key in ("content", "text"):
            v = u.get(key)
            if isinstance(v, str) and v:
                return v
        chunk = u.get("agentMessageChunk") or u.get("agent_message_chunk")
        if isinstance(chunk, dict):
            c = chunk.get("content") or chunk.get("text")
            if isinstance(c, dict):
                return c.get("text", "")
            if isinstance(c, str):
                return c
        c = u.get("content")
        if isinstance(c, dict):
            return c.get("text", "")
        return ""

    def _wait(self, mid, timeout):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if mid in self.pending:
                    return self.pending.pop(mid)
            time.sleep(0.03)
        return None

    def start(self):
        binp = _kiro_bin()
        if not binp:
            raise RuntimeError("kiro-cli not found on PATH (set KIRO_BIN)")
        cmd = [binp, "acp", "--trust-all-tools"]
        kwargs = {"env": dict(os.environ)}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0, **kwargs)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        if not self._initialize():
            raise RuntimeError("ACP initialize failed")
        self._open_session()

    def _initialize(self):
        mid = self._send("initialize", {"protocolVersion": 1,
                         "clientCapabilities": {"fs": {"readTextFile": False,
                                                       "writeTextFile": False}}})
        return self._wait(mid, _INIT_TIMEOUT) is not None

    def _open_session(self):
        mid = self._send("session/new", {"cwd": os.getcwd(), "mcpServers": []})
        res = self._wait(mid, _INIT_TIMEOUT)
        if res and "result" in res:
            self.session = (res["result"] or {}).get("sessionId")

    def prompt(self, text, emit=None, timeout=_PROMPT_TIMEOUT, images=None):
        """Run one prompt; stream each token chunk via emit('delta', {'text':..})
        if given. Returns {ok, content}. Serialized per driver.

        ``images`` (optional) is a list of {mimeType, data(base64)} blocks
        appended to the ACP prompt as `{"type":"image",...}` content parts —
        kiro-cli advertises promptCapabilities.image, so this is how an image
        reaches the model (e.g. OCR / describe)."""
        with self._prompt_lock:
            if not self.session:
                return {"ok": False, "error": "no kiro session", "error_category": "unavailable"}
            self._drain()
            parts = [{"type": "text", "text": text}]
            for im in (images or []):
                if isinstance(im, dict) and im.get("data"):
                    parts.append({"type": "image",
                                  "mimeType": im.get("mimeType") or "image/png",
                                  "data": im["data"]})
            mid = self._send("session/prompt",
                             {"sessionId": self.session, "prompt": parts})
            chunks = []
            end = time.time() + timeout

            def collect():
                for ev in self._drain():
                    t = self._extract(ev.get("params", {}) or {})
                    if t:
                        chunks.append(t)
                        if emit:
                            try:
                                emit("delta", {"text": t})
                            except Exception:  # noqa: BLE001
                                pass

            while time.time() < end:
                collect()
                with self.lock:
                    done = mid in self.pending
                if done:
                    self.pending.pop(mid, None)
                    grace = time.time() + 1.5
                    last = -1
                    while time.time() < grace:
                        collect()
                        if len(chunks) == last:
                            break
                        last = len(chunks)
                        time.sleep(0.08)
                    collect()
                    return {"ok": True, "content": "".join(chunks).strip()}
                if not self.alive:
                    return {"ok": True, "content": "".join(chunks).strip()}
                time.sleep(0.05)
            collect()
            return {"ok": True, "content": "".join(chunks).strip()}

    def stop(self):
        self.alive = False
        try:
            if self.proc:
                self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    def reset_session(self):
        try:
            self._open_session()
        except Exception:  # noqa: BLE001
            return False
        self._system_sent = False
        with self.lock:
            self.events = []
            self.pending = {}
        return bool(self.session)


# --------------------------------------------------------------------------- #
# Singleton (interactive/default) + warm pool (one-shot + pinned).
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()
_DRIVER = None


def _driver(start=True):
    global _DRIVER
    with _LOCK:
        if _DRIVER is not None and _DRIVER.alive:
            return _DRIVER
        if not start:
            return None
        _DRIVER = _Driver()
    try:
        _DRIVER.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[acp] kiro start failed: {exc}", flush=True)
        with _LOCK:
            _DRIVER = None
        return None
    return _DRIVER


def _pool_size():
    try:
        n = int(os.environ.get("KIRO_POOL_SIZE", "3"))
    except ValueError:
        n = 3
    return max(0, min(n, 8))


_POOL_LOCK = threading.Lock()
_POOL = None


class _Pool:
    """Bounded pool of warm _Driver processes. One-shot (idle queue) + pinned
    (sticky, keyed by pool_id) share one capacity (``size``)."""

    def __init__(self, size):
        self.size = size
        self._idle = _queue.Queue()
        self._pinned = {}
        self._pinned_ts = {}      # pool_id -> last-use epoch (for idle-TTL eviction)
        self._busy = {}           # pool_id -> prompts running on it (never evicted while > 0)
        self._created = 0
        self._cv = threading.Condition()

    def _spawn(self):
        d = _Driver()
        d.start()
        return d

    def lease(self, timeout=None):
        if timeout is None:
            timeout = _lease_timeout()
        try:
            d = self._idle.get_nowait()
            if d.alive and d.session:
                return d
            try:
                d.stop()
            except Exception:  # noqa: BLE001
                pass
            with self._cv:
                self._created -= 1
        except _queue.Empty:
            pass
        with self._cv:
            make = self._created < self.size
            if make:
                self._created += 1
        if make:
            try:
                return self._spawn()
            except Exception as exc:  # noqa: BLE001
                with self._cv:
                    self._created -= 1
                raise exc
        d = self._idle.get(timeout=timeout)
        if not (d.alive and d.session):
            try:
                d.stop()
            except Exception:  # noqa: BLE001
                pass
            with self._cv:
                self._created -= 1
            return self._spawn()
        return d

    def release(self, d, healthy=True):
        if d is None:
            return
        if healthy and d.alive and d.reset_session():
            self._idle.put(d)
            return
        try:
            d.stop()
        except Exception:  # noqa: BLE001
            pass
        with self._cv:
            self._created = max(0, self._created - 1)

    def pinned(self, pool_id, timeout=None):
        if timeout is None:
            timeout = _lease_timeout()
        # Reclaim any pinned driver idle past the TTL before we weigh capacity —
        # a long-idle sticky driver shouldn't hold a slot forever.
        self.evict_idle()
        with self._cv:
            d = self._pinned.get(pool_id)
            if d is not None and d.alive and d.session:
                self._pinned_ts[pool_id] = time.time()
                self._busy[pool_id] = self._busy.get(pool_id, 0) + 1    # released by touch_pinned
                return d
            if d is not None:
                try:
                    d.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pinned.pop(pool_id, None)
                self._pinned_ts.pop(pool_id, None)
                self._created = max(0, self._created - 1)
            at_cap = self._created >= self.size
            if not at_cap:
                self._created += 1
        if at_cap:
            # IMPROVEMENTS_NEXT #9: a full pool used to refuse a NEW pool_id until someone released one by hand.
            # Free a slot in order: a warm one-shot driver; else the LEAST-recently-used pinned driver that is not
            # mid-prompt and idle >= KIRO_POOL_EVICT_MIN_IDLE (default 30 s); else wait for a one-shot release.
            idle = None
            try:
                idle = self._idle.get_nowait()
            except _queue.Empty:
                if self._evict_lru_pinned(exclude=pool_id):
                    idle = True
            if idle is None:
                try:
                    idle = self._idle.get(timeout=timeout)
                except _queue.Empty:
                    raise RuntimeError("kiro pool at capacity (%d, all pinned drivers busy); no slot for pool_id %r"
                                       % (self.size, pool_id))
            if idle is not True:
                try:
                    idle.stop()
                except Exception:  # noqa: BLE001
                    pass
        try:
            nd = self._spawn()
        except Exception as exc:  # noqa: BLE001
            with self._cv:
                if not at_cap:
                    self._created = max(0, self._created - 1)
            raise exc
        with self._cv:
            self._pinned[pool_id] = nd
            self._pinned_ts[pool_id] = time.time()
            self._busy[pool_id] = self._busy.get(pool_id, 0) + 1        # released by touch_pinned
        return nd

    def _evict_lru_pinned(self, exclude=None):
        """Stop the least-recently-used pinned driver that is not mid-prompt and has been idle at least
        KIRO_POOL_EVICT_MIN_IDLE seconds; its slot passes to the caller (the created count is unchanged).
        -> the evicted pool_id or None."""
        min_idle = _env_int("KIRO_POOL_EVICT_MIN_IDLE", 30, lo=0)
        now = time.time()
        with self._cv:
            cands = sorted((ts, pid) for pid, ts in self._pinned_ts.items()
                           if pid != exclude and not self._busy.get(pid) and now - ts >= min_idle)
            if not cands:
                return None
            pid = cands[0][1]
            d = self._pinned.pop(pid, None)
            self._pinned_ts.pop(pid, None)
        if d is not None:
            print("[acp] pool full: evicted the idle pinned driver %r (LRU)" % pid, flush=True)
            try:
                d.stop()
            except Exception:  # noqa: BLE001
                pass
        return pid

    def touch_pinned(self, pool_id):
        """The prompt finished: mark the pinned driver as just-used (resets its idle clock) and not busy."""
        with self._cv:
            if self._busy.get(pool_id):
                self._busy[pool_id] -= 1
                if not self._busy[pool_id]:
                    self._busy.pop(pool_id, None)
            if pool_id in self._pinned:
                self._pinned_ts[pool_id] = time.time()

    def evict_idle(self):
        """Stop + drop any PINNED driver idle longer than KIRO_POOL_IDLE_TTL.
        Returns the list of evicted pool_ids. No-op when the TTL is 0."""
        ttl = _idle_ttl()
        if ttl <= 0:
            return []
        now = time.time()
        victims = []
        with self._cv:
            for pid, ts in list(self._pinned_ts.items()):
                if now - ts > ttl and not self._busy.get(pid):
                    victims.append(pid)
            for pid in victims:
                d = self._pinned.pop(pid, None)
                self._pinned_ts.pop(pid, None)
                if d is not None:
                    self._created = max(0, self._created - 1)
                    try:
                        d.stop()
                    except Exception:  # noqa: BLE001
                        pass
        return victims

    def release_pinned(self, pool_id):
        with self._cv:
            d = self._pinned.pop(pool_id, None)
            self._pinned_ts.pop(pool_id, None)
            if d is not None:
                self._created = max(0, self._created - 1)
        if d is not None:
            try:
                d.stop()
            except Exception:  # noqa: BLE001
                pass
        return d is not None

    def pinned_ids(self):
        with self._cv:
            return list(self._pinned.keys())

    def drain(self):
        while True:
            try:
                d = self._idle.get_nowait()
            except _queue.Empty:
                break
            try:
                d.stop()
            except Exception:  # noqa: BLE001
                pass
        with self._cv:
            for d in self._pinned.values():
                try:
                    d.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._pinned.clear()
            self._pinned_ts.clear()
            self._busy.clear()
            self._created = 0


def _pool():
    global _POOL
    if _pool_size() == 0:
        return None
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = _Pool(_pool_size())
        return _POOL


def on_unload():
    """IMPROVEMENTS_NEXT #8: the daemon calls this BEFORE it hot-reloads this module (cap:_daemon reload_cap).
    A reload re-executes the module, so `_POOL` / `_DRIVER` start as None again and every kiro-cli process of the
    old pool + the singleton used to be orphaned (16 kiro processes after one reload). Stop them all cleanly;
    the new module re-warms lazily. -> {stopped_pool, stopped_singleton}."""
    global _POOL, _DRIVER
    n = 0
    with _POOL_LOCK:
        p, _POOL = _POOL, None
    if p is not None:
        try:
            n = p._created
            p.drain()
        except Exception:  # noqa: BLE001
            pass
    single = False
    with _LOCK:
        d, _DRIVER = _DRIVER, None
    if d is not None:
        try:
            d.stop()
            single = True
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "stopped_pool": n, "stopped_singleton": single}


# --------------------------------------------------------------------------- #
# Capability methods.
# --------------------------------------------------------------------------- #
def version(args, ctx):
    binp = _kiro_bin()
    if not binp:
        return {"ok": False, "error": "kiro-cli not found", "error_category": "unavailable"}
    try:
        p = subprocess.run([binp, "--version"], capture_output=True, timeout=15)
        return {"ok": True, "version": (p.stdout or p.stderr).decode("utf-8", "replace").strip()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "error_category": "unavailable"}


def status(args, ctx):
    binp = _kiro_bin()
    st = {"ok": True, "available": bool(binp), "bin": binp or None,
          "pool_size": _pool_size(),
          "idle_ttl": _idle_ttl(), "lease_timeout": _lease_timeout(),
          "prompt_timeout": _pool_prompt_timeout() or _PROMPT_TIMEOUT}
    if _POOL is not None:
        # Opportunistically reclaim idle-expired pins so the reported counts are
        # live, then surface warm (idle+pinned created) + the pinned id list.
        try:
            _POOL.evict_idle()
        except Exception:  # noqa: BLE001
            pass
        st["pool_warm"] = _POOL._created
        st["pool_idle"] = _POOL._idle.qsize()
        st["pool_pinned"] = _POOL.pinned_ids()
    else:
        # No pool instantiated yet — report zeros (not null) so callers can render
        # the pool panel without a live lease first.
        st["pool_warm"] = 0
        st["pool_idle"] = 0
        st["pool_pinned"] = []
    with _LOCK:
        st["singleton_ready"] = bool(_DRIVER and _DRIVER.alive and _DRIVER.session)
    return st


def prompt(args, ctx, emit=None):
    """Run a kiro prompt. Streams token chunks via emit('delta',{'text':..}).

    Routing:
      * pool_id   -> a PINNED warm driver, STATEFUL across calls (multi-turn).
      * oneshot   -> a POOLED warm driver, fresh session, recycled after (sweeps).
      * else      -> the stateful SINGLETON (interactive default).
    ``system`` is prepended once per driver; ``reset`` starts a fresh session."""
    message = (args.get("message") or args.get("text") or "").strip()
    if not message:
        return {"ok": False, "error": "message required", "error_category": "application"}
    system = args.get("system") or ""
    reset = bool(args.get("reset"))
    pool_id = args.get("pool_id")
    oneshot = bool(args.get("oneshot"))
    images = args.get("images") or None
    replay = str(args.get("replay") or "")
    expect_session = args.get("expect_session") or None
    _caller_timeout = args.get("timeout")
    timeout = int(_caller_timeout or _PROMPT_TIMEOUT)
    # For POOLED prompts (pinned/oneshot), an explicit caller timeout wins; else
    # the pool's hard per-prompt budget (KIRO_POOL_PROMPT_TIMEOUT) applies when
    # set, otherwise the default. Keeps a wedged pooled prompt from hanging the
    # slot indefinitely without affecting the interactive singleton.
    _pooled_budget = _pool_prompt_timeout()
    pooled_timeout = int(_caller_timeout or _pooled_budget or _PROMPT_TIMEOUT)

    def _with_system(d, text):
        if system and not getattr(d, "_system_sent", False):
            d._system_sent = True
            return system.strip() + "\n\n" + text
        return text

    # PINNED (sticky) stateful driver
    if pool_id:
        pool = _pool()
        if pool is not None:
            try:
                d = pool.pinned(str(pool_id))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": "kiro pool: %s" % exc, "error_category": "unavailable"}
            try:
                if reset:
                    d.reset_session()
                r = d.prompt(_with_system(d, _maybe_replay(d, message, replay, expect_session)), emit=emit,
                             timeout=pooled_timeout, images=images)
            finally:
                try:
                    pool.touch_pinned(str(pool_id))            # also clears its busy mark
                except Exception:  # noqa: BLE001
                    pass
            if r.get("ok"):
                r["pool_id"] = str(pool_id)
            if isinstance(r, dict):
                r["session"] = getattr(d, "session", None)
            return _replay_note(d, r)

    # ONE-SHOT pooled driver (parallel, clean context)
    if oneshot:
        pool = _pool()
        if pool is not None:
            d = None
            try:
                d = pool.lease()
            except Exception as exc:  # noqa: BLE001
                print(f"[acp] pool lease failed, using singleton: {exc}", flush=True)
                d = None
            if d is not None:
                text = (system.strip() + "\n\n" + message) if system else message
                healthy = True
                try:
                    r = d.prompt(text, emit=emit, timeout=pooled_timeout, images=images)
                except Exception as exc:  # noqa: BLE001
                    healthy = False
                    r = {"ok": False, "error": str(exc), "error_category": "application"}
                finally:
                    pool.release(d, healthy=healthy and bool(r.get("ok")))
                if r.get("ok"):
                    r["pooled"] = True
                return r

    # SINGLETON (interactive default)
    if reset:
        _stop_singleton()
    d = _driver(start=True)
    if d is None:
        return {"ok": False, "error": "kiro-cli unavailable", "error_category": "unavailable"}
    r = d.prompt(_with_system(d, _maybe_replay(d, message, replay, expect_session)), emit=emit, timeout=timeout,
                 images=images)
    if isinstance(r, dict):
        r["session"] = d.session
    return _replay_note(d, r)


def _maybe_replay(d, message, replay, expect_session):
    """CONDITIONAL conversation replay (IMPROVEMENTS_NEXT #2): the caller (llm_server's kiro backend) believes this
    driver holds the chat - it was the session ``expect_session`` - and sends the earlier turns as ``replay`` in
    case it does not. They go in front of the message only when the driver's session is not that one (a daemon /
    driver restart, a recycled pin, a host restart the caller survived from its persisted state)."""
    if not replay:
        return message
    have = getattr(d, "session", None)
    if expect_session and have and have == expect_session:
        return message
    d._replayed_chars = len(replay)                # reported back with the result (llm_server telemetry)
    return replay + message


def _replay_note(d, r):
    n = getattr(d, "_replayed_chars", 0)
    if n and isinstance(r, dict):
        r["replayed_chars"] = n
    try:
        d._replayed_chars = 0
    except Exception:  # noqa: BLE001
        pass
    return r


def release(args, ctx):
    """Release a PINNED pooled driver by pool_id (or ALL pins if none)."""
    pid = (args.get("pool_id") or "").strip() or None
    if _POOL is None:
        return {"ok": True, "released": []}
    if pid:
        ok = _POOL.release_pinned(str(pid))
        return {"ok": True, "released": [str(pid)] if ok else []}
    ids = _POOL.pinned_ids()
    for p in ids:
        _POOL.release_pinned(p)
    return {"ok": True, "released": ids}


def _stop_singleton():
    global _DRIVER
    with _LOCK:
        d = _DRIVER
        _DRIVER = None
    if d:
        d.stop()


METHODS = {"version": version, "status": status, "prompt": prompt, "release": release}
CAPABILITY = "cap:system.acp"
VARIANT = None
ATTRS = {}
