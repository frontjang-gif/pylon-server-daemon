# pylon-server-daemon

Independent Windows/Linux endpoint agents, capability modules and server supervision. Passive mode connects to an existing authenticated /agent endpoint. Server mode requires an explicit locally installed API composition via PYLON2_SERVER_ROOT (or server_root), and optionally PYLON2_SERVER_PYTHON. Do not run the legacy installation scripts against the active VM; staged unit templates need host-local configuration.

See daemons/README.md. No server code, frontend code, runtime config or credentials belong in this repository.
## Download or update the daemon

The public repository provides standalone source downloaders. Both default to
`~/pylon-server/daemon`, require no GitHub authentication, preserve host-local
configuration and data, and do not start the daemon or install dependencies.

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/frontjang-gif/pylon-server-daemon/main/install.ps1 | iex
```

Linux (Bash, requires curl and Python 3.10+):

```bash
curl -fsSL https://raw.githubusercontent.com/frontjang-gif/pylon-server-daemon/main/install.sh | bash
```

These one-liners execute downloaded code directly. Review install.ps1 or install.sh in this repository before running them. Alternatively, download the script to a file, inspect it, and run it locally. An optional PowerShell
`-Destination` or Bash first argument overrides the install directory.
The scripts download the current main branch and merge its source files;
they do not remove obsolete files or modify existing host-local configuration.
A daemon agent token and dependencies must still be configured separately.
