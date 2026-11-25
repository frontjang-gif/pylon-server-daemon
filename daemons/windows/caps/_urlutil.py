"""_urlutil - resolve an app-relative link against the framework URL this daemon talks to.

A notification's link is page-relative (``/apps/platform/log/?...``): that is right inside the browser, but the
tray pane, a toast or any OS "open" call has no page to resolve it against, so the SERVER part was missing.
The daemon already knows its framework URL (the one it dials), which is also correct when the framework is on
another machine - so it resolves here rather than the server guessing a loopback origin.

Sibling module of the tray scripts (they run as top-level scripts: ``import _urlutil``). Stdlib only.
"""
from __future__ import annotations


def absolute(url, fw):
    """``/apps/x/?a=1`` + ``http://host:8900`` -> ``http://host:8900/apps/x/?a=1``. Absolute urls (``http(s)://``,
    ``mailto:``, a protocol-relative ``//cdn/..``) and an empty url are returned unchanged."""
    s = str(url or "").strip()
    if s.startswith("/") and not s.startswith("//") and fw:
        return str(fw).strip().rstrip("/") + s
    return s
