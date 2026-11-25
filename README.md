# pylon-server-daemon

Independent Windows/Linux endpoint agents, capability modules and server supervision. Passive mode connects to an existing authenticated /agent endpoint. Server mode requires an explicit locally installed API composition via PYLON2_SERVER_ROOT (or server_root), and optionally PYLON2_SERVER_PYTHON. Do not run the legacy installation scripts against the active VM; staged unit templates need host-local configuration.

See daemons/README.md. No server code, frontend code, runtime config or credentials belong in this repository.
