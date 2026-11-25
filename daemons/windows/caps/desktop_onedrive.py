"""capability: cap:desktop.onedrive  (Windows — OneDrive sync-state + file mgmt).

Endpoint-bound: reads OneDrive placeholder/sync state from Windows file
ATTRIBUTES (the `attrib` command) and manages files (set online-only, pin,
move into OneDrive). This is OS-bound work — the `attrib` subprocess + the local
filesystem side effects live HERE, in the daemon, never in the app. The app
(apps/file/onedrive) is a thin env-agnostic front that calls this cap.

Sync-status attribute legend (from `attrib`):
  O  cloud-only placeholder (no local content)   -> 'cloud_only'
  P  pinned / always available                   -> 'always_available'
  U  unpinned (online-only requested; uploading) -> 'syncing'
  none of O/U/P                                   -> 'locally_available'

Methods:
  status()                               -> {ok, available}
  roots()                                -> {ok, roots:[...], count}
  scan({path, limit?})                   -> {ok, folder, files:[{name,status,size,...}], count}
  stat({path})                           -> {ok, file, status, ...}
  set_online_only({path})                -> {ok, path, status}   (attrib +U -P)
  pin({path})                            -> {ok, path, status}   (attrib +P -U)
  move({src, dest, online_only?})        -> {ok, src, dest, ...}

Degrades to {ok:False, error_category:unavailable} off-Windows.
"""
from __future__ import annotations

import os
import shutil
import subprocess

_WIN = (os.name == "nt")


def _na():
    return {"ok": False, "error": "OneDrive sync attributes are Windows-only on this endpoint",
            "error_category": "unavailable"}


def _detect_roots():
    roots = []
    home = os.path.expanduser("~")
    try:
        for name in os.listdir(home):
            p = os.path.join(home, name)
            if os.path.isdir(p) and "onedrive" in name.lower():
                roots.append(p)
    except OSError:
        pass
    return roots


def _status_from_attrs(filepath, attrs):
    has_o, has_p, has_u = ("O" in attrs), ("P" in attrs), ("U" in attrs)
    if has_o:
        status = "cloud_only"
    elif has_p:
        status = "always_available"
    elif has_u:
        status = "syncing"
    else:
        status = "locally_available"
    try:
        size = os.stat(filepath).st_size
    except OSError:
        size = 0
    return {"status": status, "attributes": attrs, "size": size,
            "size_on_disk": 0 if has_o else size}


def _sync_status(filepath):
    if not os.path.exists(filepath):
        return {"status": "not_found", "attributes": "", "size": 0, "size_on_disk": 0}
    attrs = ""
    try:
        r = subprocess.run(["attrib", filepath], capture_output=True, text=True, timeout=10)
        if filepath in r.stdout:
            attrs = r.stdout.strip().split(filepath)[0].strip()
    except Exception:  # noqa: BLE001
        attrs = ""
    return _status_from_attrs(filepath, attrs)


def _attrib_scan(folder):
    """{full_path: attr_flags} for every file under `folder` in ONE `attrib`
    subprocess (vs one per file)."""
    out = {}
    try:
        r = subprocess.run(["attrib", os.path.join(folder, "*")],
                           capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return out
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        idx = line.find(folder)
        if idx < 0:
            continue
        flags = line[:idx]
        path = line[idx:].strip()
        attrs = "".join(ch for ch in flags if ch.isalpha() and ch.isupper())
        out[os.path.normcase(path)] = attrs
    return out


def _attrib(flags, filepath):
    try:
        r = subprocess.run(["attrib"] + flags + [filepath],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ methods --
def status(args, ctx):
    return {"ok": True, "available": _WIN}


def roots(args, ctx):
    if not _WIN:
        return _na()
    rs = _detect_roots()
    return {"ok": True, "roots": rs, "count": len(rs)}


def scan(args, ctx):
    if not _WIN:
        return _na()
    path = (args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path required", "error_category": "application"}
    if not os.path.isdir(path):
        return {"ok": False, "error": "not a folder: %s" % path, "error_category": "application"}
    try:
        limit = int(args.get("limit") or 500)
    except (TypeError, ValueError):
        limit = 500
    # Cheap change-skip (PLATFORM_IDEAS #27): a folder scan runs a full `attrib`
    # subprocess over the listing, which dominates a monitor poll. The caller can
    # pass the `dir_mtime` it last saw; if the folder's mtime is unchanged we
    # SKIP the expensive attrib scan and return {unchanged:True}. NOTE: a dir
    # mtime bumps on entry add/remove/rename but NOT on an in-place sync-status
    # flip (cloud<->local), so this is opt-in for STRUCTURAL-change polling; the
    # full scan (no since_mtime) still catches status transitions.
    try:
        dir_mtime = os.stat(path).st_mtime
    except OSError:
        dir_mtime = None
    since = args.get("since_mtime")
    if since is not None and dir_mtime is not None:
        try:
            if abs(float(since) - dir_mtime) < 1e-6:
                return {"ok": True, "folder": path, "unchanged": True,
                        "dir_mtime": dir_mtime}
        except (TypeError, ValueError):
            pass
    attr_map = _attrib_scan(path)
    out = []
    for name in sorted(os.listdir(path))[:limit]:
        fp = os.path.join(path, name)
        if os.path.isfile(fp):
            attrs = attr_map.get(os.path.normcase(fp), "")
            out.append({"name": name, **_status_from_attrs(fp, attrs)})
    return {"ok": True, "folder": path, "files": out, "count": len(out),
            "dir_mtime": dir_mtime}


def stat(args, ctx):
    if not _WIN:
        return _na()
    path = (args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path required", "error_category": "application"}
    if not os.path.exists(path):
        return {"ok": False, "error": "not found: %s" % path, "error_category": "application"}
    return {"ok": True, "file": path, **_sync_status(path)}


def _action(args, flags, label):
    if not _WIN:
        return _na()
    path = (args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path required", "error_category": "application"}
    if not os.path.isfile(path):
        return {"ok": False, "error": "not a file: %s" % path, "error_category": "application"}
    ok = _attrib(flags, path)
    st = _sync_status(path)
    return {"ok": bool(ok), "path": path, "action": label, "status": st.get("status"),
            **({} if ok else {"error": "attrib command failed", "error_category": "application"})}


def set_online_only(args, ctx):
    return _action(args, ["+U", "-P"], "set online-only")


def pin(args, ctx):
    return _action(args, ["+P", "-U"], "pin always-available")


def move(args, ctx):
    if not _WIN:
        return _na()
    src = (args.get("src") or "").strip()
    dest = (args.get("dest") or "").strip()
    if not src or not dest:
        return {"ok": False, "error": "src and dest required", "error_category": "application"}
    if not os.path.exists(src):
        return {"ok": False, "error": "source not found: %s" % src, "error_category": "application"}
    online_only = args.get("online_only", True)
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.move(src, dest)
        res = {"src": src, "dest": dest, "size_bytes": os.path.getsize(dest)}
        if online_only:
            res["online_only_set"] = _attrib(["+U", "-P"], dest)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "move failed: %s" % exc, "error_category": "application"}
    return {"ok": True, **res}


METHODS = {"status": status, "roots": roots, "scan": scan, "stat": stat,
           "set_online_only": set_online_only, "pin": pin, "move": move}
CAPABILITY = "cap:desktop.onedrive"
VARIANT = None
ATTRS = {"desktop": True, "windows": True}
