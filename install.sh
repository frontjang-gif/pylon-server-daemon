#!/usr/bin/env bash
# Download/update the public Pylon daemon source. Does not start any services.
set -euo pipefail

dest="${1:-$HOME/pylon-server/daemon}"
url="https://github.com/frontjang-gif/pylon-server-daemon/archive/refs/heads/main.zip"

for tool in curl python3; do
    command -v "$tool" >/dev/null || { echo "Required: $tool" >&2; exit 1; }
done

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
curl --fail --show-error --silent --location --proto '=https' \
    --retry 2 --connect-timeout 15 --max-time 180 \
    --output "$tmp/daemon.zip" "$url"

python3 - "$tmp/daemon.zip" "$dest" <<'PY'
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import zipfile

archive, destination = sys.argv[1:]
root = Path(destination).expanduser().absolute()
# Do not follow symlinks/reparse-like filesystem redirects on update.
for p in [root, *root.parents]:
    if p.is_symlink():
        raise SystemExit(f"Refusing symlink destination: {p}")
with zipfile.ZipFile(archive) as z:
    if z.testzip() is not None:
        raise SystemExit("Archive integrity check failed")
    entries = {}
    for info in z.infolist():
        p = PurePosixPath(info.filename)
        if (p.is_absolute() or ".." in p.parts or "\\" in info.filename
                or not p.parts or p.parts[0] != "pylon-server-daemon-main"
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise SystemExit("Unexpected archive entry")
        if info.is_dir():
            continue
        rel = Path(*p.parts[1:])
        if not rel.parts or ":" in str(rel) or rel in entries:
            raise SystemExit("Invalid archive entry")
        if (rel.name in {"config.json", "agent-token", ".env"}
                or rel.name.startswith(".env.")
                or any(x in {"data", ".venv", ".venvs", "__pycache__"} for x in rel.parts)):
            continue
        entries[rel] = info
    required = [Path("daemons/windows/agent.py"), Path("daemons/linux/agent.py"),
                Path("daemons/serve_mode.py")]
    if not all(p in entries for p in required):
        raise SystemExit("Daemon source is missing from the archive")
    for rel in entries:
        target = root / rel
        if any(p.is_symlink() for p in [target, *target.parents]):
            raise SystemExit(f"Refusing symlink: {target}")
        if target.exists() and not target.is_file():
            raise SystemExit(f"File conflict: {target}")
        for p in target.parents:
            if p.exists() and not p.is_dir():
                raise SystemExit(f"Directory conflict: {p}")
    root.mkdir(parents=True, exist_ok=True)
    for rel, info in entries.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".pylon-download-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as out, z.open(info) as src:
                shutil.copyfileobj(src, out)
            os.chmod(temp, 0o755 if info.external_attr >> 16 & 0o111 else 0o644)
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
print(f"Daemon source installed at {root}")
print("Local configuration preserved. No daemon was started.")
PY
