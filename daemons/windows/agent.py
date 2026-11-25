"""agent.py — Windows endpoint capability daemon.

Model 2: this daemon DIALS OUT to the framework over a WebSocket and holds it
open, so it works through NAT (the framework never connects back). It registers
the capabilities this machine can provide, then serves `invoke` messages the
framework pushes down the channel, replying with correlated `result` messages.

Capability-agnostic transport here; the actual COM work lives in caps/ (the only
place win32com is imported). A capability call runs on a worker thread (its own
COM apartment) so the socket loop never blocks and concurrent invokes work.

Run:  py -3 daemons/windows/agent.py
Deps: websockets (client), pywin32 (only used inside caps/).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import pkgutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.append(os.path.dirname(_HERE))
import serve_mode  # noqa: E402  pylon-server: passive runner or bootstrap of a local server
sys.path.append(os.path.dirname(_HERE))

# Make stdout/stderr UTF-8 + error-tolerant so a non-ASCII capability name,
# room name, or error message can never crash the daemon on a cp1252 console.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _discover_capabilities():
    """Auto-discover capability modules in caps/ and build the registry from
    what the CODE actually provides. A capability module declares:
        CAPABILITY : str          e.g. "cap:com.outlook.calendar"
        METHODS    : {name: fn}    the invokable methods
        VARIANT    : str | None    optional
        ATTRS      : dict          optional
    Returns {cap_name: {methods, variant, attrs, module}}.
    """
    import caps as _caps_pkg
    registry = {}
    for mod_info in pkgutil.iter_modules(_caps_pkg.__path__):
        name = mod_info.name
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"caps.{name}")
        except Exception as exc:  # a broken cap module must not sink the daemon
            print(f"[agent] WARN: capability module caps.{name} failed to import: {exc}",
                  flush=True)
            continue
        cap = getattr(mod, "CAPABILITY", None)
        methods = getattr(mod, "METHODS", None)
        if not cap or not isinstance(methods, dict):
            continue  # not a capability module
        registry[cap] = {
            "methods": methods,
            "variant": getattr(mod, "VARIANT", None),
            "attrs": getattr(mod, "ATTRS", {}) or {},
            "module": f"caps.{name}",
        }
    # Advertise the synthetic `cap:_daemon` introspection capability so the
    # framework can ROUTE its read-only `versions` probe (which lists each live
    # cap's methods + module mtime). Without this it was handled in _dispatch but
    # never advertised, so `cap("cap:_daemon","versions")` had no endpoint to pick
    # (the /v1/endpoints/caps probe reported "no cap:_daemon"). The MUTATING
    # `reload_cap` stays gated behind PYLON2_CAP_HOTRELOAD in _dispatch — this only
    # makes the daemon's live method inventory VISIBLE (e.g. to confirm a pushed
    # cap method like com.outlook.mail `contacts` is actually live, LEAVE_APPUI #14).
    registry.setdefault("cap:_daemon", {
        "methods": {"versions": True, "reload_cap": True},  # dispatched specially
        "variant": None,
        "attrs": {"introspection": True, "readonly_default": True},
        "module": "agent._daemon",
    })
    return registry


def _validate_against_manifest(registry, manifest_path):
    """Drift guard: the manifest's provides_capabilities MUST match what the code
    registers. Warn on either direction so the declaration and the running code
    can never silently diverge (the AppForge capabilities-drift lesson)."""
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        print("[agent] WARN: no/invalid manifest.json; skipping drift check", flush=True)
        return
    declared = {c.get("name") for c in (manifest.get("provides_capabilities") or [])}
    provided = set(registry)
    provided.discard("cap:_daemon")     # synthetic introspection cap, not a caps/ module
    missing_impl = declared - provided   # manifest promises, no code
    undeclared = provided - declared     # code provides, not in manifest
    for cap in sorted(missing_impl):
        print(f"[agent] DRIFT: manifest declares {cap} but NO capability module "
              f"provides it", flush=True)
    for cap in sorted(undeclared):
        print(f"[agent] DRIFT: capability {cap} is provided by code but NOT "
              f"declared in manifest.provides_capabilities", flush=True)
    if not missing_impl and not undeclared:
        print(f"[agent] manifest OK: matches code ({len(provided)} capabilities)",
              flush=True)


def _check_pip_deps(manifest_path):
    """Warn on any pip dependency the manifest declares but that isn't importable
    on this endpoint. The manifest's `pip` map is {pip_name: reason}; we map the
    common pip→import name differences and probe each. A missing dep is only a
    WARNING — the owning capability already degrades to a clean 'unavailable' at
    call time, so the daemon still starts and serves every cap whose deps ARE
    present."""
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return
    pip = manifest.get("pip") or {}
    if not pip:
        return
    import importlib.util as _ilu
    # pip distribution name -> importable module name (when they differ).
    _IMPORT_NAME = {"Pillow": "PIL", "pywin32": "win32api",
                    "websocket-client": "websocket", "pywebview": "webview",
                    "beautifulsoup4": "bs4", "pyyaml": "yaml"}
    missing = []
    for dist in pip:
        mod = _IMPORT_NAME.get(dist, dist.replace("-", "_"))
        try:
            found = _ilu.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(dist)
    if missing:
        print(f"[agent] WARN: missing pip deps {missing} — the capabilities that "
              f"need them will report 'unavailable'. Install: py -3 -m pip install "
              + " ".join(missing), flush=True)
    else:
        print(f"[agent] pip deps OK: all {len(pip)} declared dependencies present",
              flush=True)


# Built once at startup from the caps/ package, validated against the manifest.
CAPABILITIES = _discover_capabilities()
_validate_against_manifest(CAPABILITIES, os.path.join(_HERE, "manifest.json"))
_check_pip_deps(os.path.join(_HERE, "manifest.json"))


def _reload_cap(mod_name):
    """DEV hot-reload (#18): re-import a single caps/<mod> module and rebuild its
    CAPABILITIES entry WITHOUT dropping the WS or restarting the daemon — so a
    system_cdp edit iterates in ~1s instead of a ~15s restart+reconnect.

    Re-imports caps.<mod_name> via importlib.reload and swaps the entry keyed by
    the module's CAPABILITY. Returns {ok, cap, methods} or {ok:False, error}.
    Only reloads the one named module; shared imports it pulls in are refreshed
    only if they too are reloaded (good enough for iterating a single cap)."""
    import importlib as _il
    name = (mod_name or "").strip()
    if not name or name.startswith("_") or "." in name or "/" in name or "\\" in name:
        return {"ok": False, "error": "bad module name %r (use the bare caps module, e.g. 'system_cdp')" % mod_name}
    try:
        mod = _il.import_module("caps.%s" % name)
        unloaded = None
        hook = getattr(mod, "on_unload", None)      # IMPROVEMENTS_NEXT #8: let the OLD module release its children
        if callable(hook):
            try:
                unloaded = hook()
            except Exception as exc:  # noqa: BLE001
                unloaded = {"ok": False, "error": "on_unload failed: %s" % exc}
        mod = _il.reload(mod)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "reload failed: %s: %s" % (type(exc).__name__, exc)}
    cap = getattr(mod, "CAPABILITY", None)
    methods = getattr(mod, "METHODS", None)
    if not cap or not isinstance(methods, dict):
        return {"ok": False, "error": "caps.%s is not a capability module "
                "(no CAPABILITY/METHODS)" % name}
    # Drop any stale entry that pointed at the OLD module object for this cap,
    # then install the fresh one.
    for c, prov in list(CAPABILITIES.items()):
        if prov.get("module") == "caps.%s" % name and c != cap:
            CAPABILITIES.pop(c, None)
    CAPABILITIES[cap] = {"methods": methods, "variant": getattr(mod, "VARIANT", None),
                         "attrs": getattr(mod, "ATTRS", {}) or {}, "module": "caps.%s" % name}
    print("[agent] hot-reloaded caps.%s -> %s (%d methods)"
          % (name, cap, len(methods)), flush=True)
    return {"ok": True, "cap": cap, "methods": sorted(methods.keys()), "module": "caps.%s" % name,
            **({"unloaded": unloaded} if unloaded is not None else {})}


def _load_config():
    cfg = {}
    p = os.path.join(_HERE, "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    # Host-local override, outside source. Environment still takes precedence.
    from pathlib import Path
    repo=Path(_HERE).parents[1]
    default_data=repo/'data' if (repo/'framework').is_dir() else Path.home()/'.pylon2'/'data'
    local=Path(os.environ.get('PYLON2_DATA_DIR') or default_data)/'host-config'/'daemons'/'windows'/'config.json'
    if local.exists():
        try:
            if local.resolve()!=local.absolute() or local.stat().st_size>1024*1024:raise ValueError()
            with local.open(encoding='utf-8') as f:override=json.load(f)
            if not isinstance(override,dict):raise ValueError()
            cfg.update(override)
        except (OSError,ValueError):
            raise ValueError('Invalid Windows daemon host configuration') from None
    cfg["framework_url"] = (os.environ.get("PYLON2_FRAMEWORK_URL")
                            or cfg.get("framework_url") or "ws://127.0.0.1:8900/agent")
    cfg["endpoint_id"] = (os.environ.get("PYLON2_ENDPOINT_ID")
                          or cfg.get("endpoint_id") or "win-local")
    # Edge DevTools debug endpoint for the browser capabilities. Precedence:
    # env CDP_DEBUG_URL (handled inside _cdp_common.configure) > config file >
    # the built-in default. Surface the config value so main() can apply it.
    cfg["cdp_debug_url"] = (os.environ.get("CDP_DEBUG_URL")
                            or cfg.get("cdp_debug_url") or "")
    return cfg


def _register_message(cfg):
    return {
        "type": "register",
        "endpoint": cfg["endpoint_id"],
        "platform": "windows",
        "capabilities": [
            {"name": name, "variant": c["variant"], "attrs": c["attrs"]}
            for name, c in CAPABILITIES.items()
        ],
        "meta": {"version": "1", "user": os.environ.get("USERNAME", "")},
    }


def _safe_warmup(cap, fn):
    """Run a capability's warmup() on a worker thread; log + swallow errors."""
    try:
        r = fn({}, {})
        if isinstance(r, dict) and r.get("ok"):
            print(f"[agent] warmup OK: {cap}", flush=True)
        else:
            print(f"[agent] warmup skipped: {cap} ({(r or {}).get('error', '?')})",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] warmup failed: {cap} ({exc})", flush=True)


async def _dispatch(msg, ws=None, loop=None):
    """Run one invoke (on a worker thread for COM) and build the result msg.

    STREAMING (additive): if the invoke set ``stream:true`` AND the target method
    accepts an ``emit`` keyword, we pass ``emit(event, data)`` into it. The method
    (running on a worker thread) calls emit(...) to push interim ``stream`` frames
    for this correlation id over the SAME socket, thread-safely scheduled on the
    event loop. A single terminal ``result`` still follows. Methods that don't
    accept ``emit`` are called exactly as before — no behavior change."""
    rid = msg.get("id")
    cap = msg.get("cap")
    method = msg.get("method")
    args = msg.get("args") or {}
    ctx = msg.get("ctx") or {}
    # DEV meta-capability (#18): hot-reload a caps/ module without a restart.
    #   cap:_daemon reload_cap {module:"system_cdp"}  -> re-import + re-register.
    # Gated by PYLON2_CAP_HOTRELOAD=1 so it's a deliberate dev affordance, never
    # on in a normal endpoint. Returns the fresh method list.
    if cap == "cap:_daemon":
        # READ-ONLY introspection (REMOTE_RUNTIME #8) is allowed WITHOUT the
        # hot-reload gate — it mutates nothing and is exactly what you need to SEE
        # whether the live daemon has the code you just edited (endpoint id +
        # per-cap method list + module file mtime), instead of inferring it from a
        # 'no method' error. The MUTATING methods (reload_cap) stay gated below.
        if method == "versions":
            import os.path as _op
            caps_out = {}
            for c, prov in sorted(CAPABILITIES.items()):
                modname = prov.get("module") or ""
                mtime = None
                path = None
                try:
                    m = sys.modules.get(modname)
                    path = getattr(m, "__file__", None)
                    if path and _op.isfile(path):
                        mtime = round(_op.getmtime(path), 2)
                except Exception:  # noqa: BLE001
                    pass
                caps_out[c] = {"methods": sorted((prov.get("methods") or {}).keys()),
                               "module": modname, "mtime": mtime, "path": path}
            return {"type": "result", "id": rid, "ok": True,
                    "result": {"ok": True,
                               "endpoint": (os.environ.get("PYLON2_ENDPOINT_ID") or "win-local"),
                               "pid": os.getpid(), "caps": caps_out,
                               "count": len(caps_out)}}
        if method == "list_caps":
            return {"type": "result", "id": rid, "ok": True,
                    "result": {"ok": True, "caps": sorted(CAPABILITIES.keys())}}
        if (os.environ.get("PYLON2_CAP_HOTRELOAD") or "").strip().lower() not in (
                "1", "true", "yes", "on"):
            return {"type": "result", "id": rid, "ok": False,
                    "error": "cap hot-reload disabled (set PYLON2_CAP_HOTRELOAD=1 on the daemon)",
                    "error_category": "unavailable"}
        if method == "reload_cap":
            r = _reload_cap(args.get("module") or args.get("name") or "")
            return {"type": "result", "id": rid, "ok": bool(r.get("ok")),
                    "result": r, **({} if r.get("ok") else
                                    {"error": r.get("error", "reload failed"),
                                     "error_category": "application"})}
        return {"type": "result", "id": rid, "ok": False,
                "error": "no method '%s' on cap:_daemon (have: versions, list_caps, reload_cap)" % method,
                "error_category": "application"}
    provider = CAPABILITIES.get(cap)
    if provider is None:
        return {"type": "result", "id": rid, "ok": False,
                "error": f"capability '{cap}' not provided here",
                "error_category": "unavailable"}
    fn = provider["methods"].get(method)
    if fn is None:
        return {"type": "result", "id": rid, "ok": False,
                "error": f"no method '{method}' on '{cap}'",
                "error_category": "application"}
    # Decide whether to run this call in STREAMING mode: the caller asked for it
    # AND the method opts in by accepting an `emit` parameter.
    want_stream = bool(msg.get("stream")) and ws is not None and loop is not None
    emit = None
    if want_stream:
        try:
            import inspect as _inspect
            accepts_emit = "emit" in _inspect.signature(fn).parameters
        except (TypeError, ValueError):
            accepts_emit = False
        if accepts_emit:
            # Bounded BACKPRESSURE for streaming: emit() runs on the cap's worker
            # thread and can produce frames faster than a slow/stalled socket
            # drains them. Each frame is scheduled on the loop via
            # run_coroutine_threadsafe; we track OUTSTANDING sends in `inflight`
            # and, when the backlog exceeds a high-water mark, briefly BLOCK the
            # producer thread (it's a worker thread — safe to sleep) until the
            # socket catches up. This propagates backpressure to the token reader
            # WITHOUT dropping frames or ever blocking the event loop. A bounded
            # total wait avoids wedging forever on a dead socket.
            import threading as _threading
            inflight = {"n": 0}
            inflight_lock = _threading.Lock()
            _HWM = int(os.environ.get("ACP_STREAM_HWM", "64"))      # max queued sends
            _MAX_WAIT_S = float(os.environ.get("ACP_STREAM_MAX_WAIT_S", "5.0"))

            def emit(event, data, _rid=rid):
                """Thread-safe, backpressured: schedule a `stream` frame send on
                the loop; if too many are already queued, wait (bounded) for the
                socket to drain before scheduling more."""
                frame = {"type": "stream", "id": _rid, "event": event, "data": data}

                async def _send():
                    try:
                        await ws.send(json.dumps(frame))
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        with inflight_lock:
                            inflight["n"] -= 1

                # backpressure: wait (bounded) while the send backlog is high
                waited = 0.0
                while True:
                    with inflight_lock:
                        if inflight["n"] < _HWM:
                            inflight["n"] += 1
                            break
                    if waited >= _MAX_WAIT_S:
                        # socket is stuck; schedule anyway (don't wedge the cap)
                        with inflight_lock:
                            inflight["n"] += 1
                        break
                    time.sleep(0.01)
                    waited += 0.01
                try:
                    asyncio.run_coroutine_threadsafe(_send(), loop)
                except Exception:  # noqa: BLE001
                    with inflight_lock:
                        inflight["n"] -= 1
    try:
        # COM/subprocess is blocking -> run in a thread. Pass emit only when the
        # method supports streaming (else keep the exact legacy call shape).
        if emit is not None:
            result = await asyncio.to_thread(fn, args, ctx, emit)
        else:
            result = await asyncio.to_thread(fn, args, ctx)
        ok = not (isinstance(result, dict) and result.get("ok") is False)
        if ok:
            return {"type": "result", "id": rid, "ok": True, "result": result}
        return {"type": "result", "id": rid, "ok": False,
                "error": result.get("error", "capability error"),
                "error_category": result.get("error_category", "application")}
    except Exception as exc:  # noqa: BLE001
        return {"type": "result", "id": rid, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "error_category": "application"}


async def _serve(ws, cfg):
    import websockets
    await ws.send(json.dumps(_register_message(cfg)))
    reg = json.loads(await ws.recv())
    hb = int(reg.get("heartbeat_interval", 30)) if isinstance(reg, dict) else 30
    print(f"[agent] registered as {cfg['endpoint_id']}; heartbeat {hb}s; "
          f"capabilities={list(CAPABILITIES)}", flush=True)

    # Best-effort warmup: pay one-time COM attach / MAPI costs off the first
    # real user call. Each capability may expose a 'warmup' method; run them on
    # worker threads so the socket loop isn't blocked. Failures are ignored
    # (headless box / Outlook not running → the cap degrades at call time).
    for _cap, _prov in CAPABILITIES.items():
        _warm = _prov["methods"].get("warmup")
        if _warm is not None:
            asyncio.create_task(asyncio.to_thread(_safe_warmup, _cap, _warm))

    stop = asyncio.Event()

    async def _heartbeat():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=hb)
            except asyncio.TimeoutError:
                try:
                    await ws.send(json.dumps({"type": "heartbeat",
                                              "endpoint": cfg["endpoint_id"]}))
                except Exception:
                    return

    async def _reader():
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "invoke":
                # Handle each invoke concurrently; reply when done. Pass the
                # socket + loop so a streaming method can push interim frames.
                _loop = asyncio.get_event_loop()

                async def _run(m=msg, _ws=ws, _lp=_loop):
                    res = await _dispatch(m, ws=_ws, loop=_lp)
                    try:
                        await _ws.send(json.dumps(res))
                    except Exception:
                        pass
                asyncio.create_task(_run())
        stop.set()

    from logging_bridge import Relay
    relay_task = asyncio.create_task(Relay().run(ws))
    hb_task = asyncio.create_task(_heartbeat())
    try:
        await _reader()
    finally:
        stop.set()
        hb_task.cancel()
        relay_task.cancel()
        await asyncio.gather(hb_task, relay_task, return_exceptions=True)


async def main():
    import websockets
    cfg = _load_config()
    # Server mode starts (and supervises) pylon-server BEFORE the tray, so the
    # tray opens this server's pages and keeps its own helper (port file).
    url, headers, server = serve_mode.setup(cfg, "ws://127.0.0.1:8900/agent",
                                            log=lambda m: print(m, flush=True))
    if server:
        os.environ["PYLON2_FRAMEWORK_HTTP"] = server.http_url
        os.environ.setdefault("PYLON2_TRAY_NAME", "pylon-server")
        os.environ.setdefault("PYLON2_TRAY_PORT_FILE", os.path.join(
            os.environ.get("TEMP") or ".", "pylon-server-tray-%d.port" % server.port))
    # Point the browser capabilities at an Edge DevTools endpoint. Precedence:
    #   1. env CDP_DEBUG_URL (handled in _cdp_common.configure)
    #   2. config cdp_debug_url, if it's an explicit http(s) URL (attach to a
    #      browser you manage yourself)
    #   3. the MANAGED dedicated profile — launch a persistent headless Edge on
    #      data/daemons/windows/edge-profile, seeded once from the real signed-in
    #      Edge. This is the default (cdp_debug_url absent or "auto"/"managed").
    try:
        import caps._cdp_common as _cdp
        cfg_url = (cfg.get("cdp_debug_url") or "").strip()
        if os.environ.get("CDP_DEBUG_URL"):
            _cdp.configure(None)  # env already won at import
            print(f"[agent] cdp debug endpoint (env): {_cdp.DEBUG_URL}", flush=True)
        elif cfg_url and cfg_url.lower() not in ("auto", "managed"):
            _cdp.configure(cfg_url)
            print(f"[agent] cdp debug endpoint (config): {_cdp.DEBUG_URL}", flush=True)
        else:
            import caps._edge_profile as _edge
            seeded = _edge.is_seeded()
            # #19: optionally PRE-WARM non-headless at startup so the first
            # device_login doesn't pay the cold headless->non-headless flip
            # (ensure_mode mid-flow relaunches Edge, which cost the first sign-in
            # of a session a launch). Off by default (headless is cheaper for
            # idle); turn on with config edge_prewarm_nonheadless / env
            # PYLON2_EDGE_PREWARM_NONHEADLESS=1 on a box that signs in often.
            _prewarm_nh = (bool(cfg.get("edge_prewarm_nonheadless"))
                           or bool(os.environ.get("PYLON2_EDGE_PREWARM_NONHEADLESS")))
            _hl = False if _prewarm_nh else cfg.get("edge_headless", True)
            dbg = _edge.ensure_running(headless=_hl)
            if dbg:
                _cdp.configure(dbg)
                print(f"[agent] cdp debug endpoint (managed profile, "
                      f"{'reused' if seeded else 'seeded'}"
                      f"{', pre-warmed non-headless' if _prewarm_nh else ''}): {dbg} "
                      f"[{_edge.PROFILE_DIR}]", flush=True)
            else:
                print("[agent] WARN: could not launch managed Edge; browser caps "
                      f"will use default {_cdp.DEBUG_URL} (start Edge with "
                      "--remote-debugging-port=9222, or set cdp_debug_url)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] WARN: could not configure cdp endpoint: {exc}", flush=True)

    # Start keep-awake independently of any page visit or capability invocation.
    if "cap:desktop.wakeup" in CAPABILITIES:
        import caps.desktop_wakeup as wakeup
        wakeup.start_background()

    # Auto-start the system-tray icon (cap:desktop.tray) so the platform mark is
    # present the moment the daemon is up — no manual `show` call, and it comes
    # back on every daemon restart. Gated by config/env (default ON); a headless
    # / no-desktop session (or missing pystray) just logs and carries on.
    _tray_auto = cfg.get("tray_autostart", True)
    if os.environ.get("PYLON2_TRAY_AUTOSTART", "").strip().lower() in ("0", "false", "no"):
        _tray_auto = False
    if _tray_auto and "cap:desktop.tray" in CAPABILITIES:
        try:
            r = CAPABILITIES["cap:desktop.tray"]["methods"]["show"]({}, {})
            if isinstance(r, dict) and r.get("ok"):
                print("[agent] tray icon started (cap:desktop.tray)", flush=True)
            else:
                print(f"[agent] tray not started: {(r or {}).get('error', '?')}",
                      flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] WARN: tray autostart failed: {exc}", flush=True)

    if server and _tray_auto and "cap:desktop.tray" in CAPABILITIES:
        _pylon_tray_menu(server)
    backoff_min = float(cfg.get("reconnect_min_s", 1))
    backoff_max = float(cfg.get("reconnect_max_s", 30))
    # A connection must STAY UP at least this long to count as "stable" and reset
    # the backoff. Without this, a reconnect storm (the host immediately supersedes
    # / drops each freshly-accepted channel) resets backoff to the 1s floor on every
    # cycle, producing a fixed ~1s retry hammer. Treating a sub-stable session as a
    # failure keeps the exponential backoff growing until the storm subsides.
    stable_after_s = float(cfg.get("reconnect_stable_s", 15))
    backoff = backoff_min
    print(f"[agent] connecting to {url} …", flush=True)
    try:
        await _connect_loop(url, headers, cfg, backoff_min, backoff_max, stable_after_s)
    finally:
        if server:
            server.stop()


def _pylon_tray_menu(server):
    """Server mode: pylon-server entries in the tray (the browser signs in)."""
    tray = CAPABILITIES["cap:desktop.tray"]["methods"]
    base = server.http_url
    try:
        items = [{"label": "Terminal", "url": base + "/apps/system/terminal/xterm.htm"},
                 {"label": "Server status", "url": base + "/_pylon/status"}]
        if not server.extra_env.get("PYLON_LOCAL_OWNER"):
            items.insert(0, {"label": "Sign in", "url": base + "/_pylon/login"})
        tray["set_menu"]({"items": items}, {})    # "Open pylon-server" is the tray's own entry
        tray["set_tooltip"]({"text": "pylon-server (port %d)" % server.port}, {})
        print("[agent] tray: pylon-server menu for %s" % base, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] WARN: tray menu failed: {exc}", flush=True)


async def _connect_loop(url, headers, cfg, backoff_min, backoff_max, stable_after_s):
    import websockets
    backoff = backoff_min
    while True:
        session_start = time.time()
        try:
            async with serve_mode.ws_connect(websockets.connect, url, headers, max_size=16 * 1024 * 1024) as ws:
                await _serve(ws, cfg)
            print("[agent] channel closed; reconnecting …", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] connect failed: {exc}; retrying in {backoff:.0f}s", flush=True)
        uptime = time.time() - session_start
        if uptime >= stable_after_s:
            # The channel was genuinely stable — reset to the floor.
            backoff = backoff_min
        else:
            # Flapping / immediately-dropped channel — grow the backoff so we stop
            # hammering the host during a reconnect storm.
            backoff = min(backoff_max, max(backoff, backoff_min) * 2)
        time.sleep(backoff)


if __name__ == "__main__":
    _stop_edge_on_exit = False
    try:
        # Decide now whether to reap the managed Edge on exit (default: keep it,
        # so a daemon restart reuses it fast; opt in via config/env to reap).
        try:
            _cfg0 = _load_config()
            _stop_edge_on_exit = bool(_cfg0.get("edge_stop_on_exit")) or \
                os.environ.get("PYLON2_EDGE_STOP_ON_EXIT", "").strip().lower() in ("1", "true", "yes")
        except Exception:  # noqa: BLE001
            pass
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[agent] stopped", flush=True)
    finally:
        if _stop_edge_on_exit:
            try:
                import caps._edge_profile as _edge
                _edge.stop()
                print("[agent] managed Edge stopped (edge_stop_on_exit)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] WARN: could not stop managed Edge: {exc}", flush=True)
