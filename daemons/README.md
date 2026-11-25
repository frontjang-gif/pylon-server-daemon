# daemons — endpoint daemons (per OS)

Copied from ai-pylon2 (`daemons/`, HEAD 3bf3a292, no local changes there) with
two pylon-server additions: `serve_mode.py` (passive runner or bootstrap of a
local server, and the agent-token header) wired into `linux/agent.py` and
`windows/agent.py`. See `framework/aiweb/README.md` "Daemons" for the modes,
the agent token and which machine runs what.

    python3 daemons/linux/agent.py                    # passive: PYLON2_FRAMEWORK_URL + PYLON2_AGENT_TOKEN
    PYLON2_DAEMON_MODE=server PYLON2_SERVER_ROOT=/path/to/release python3 daemons/linux/agent.py
    py -3 daemons\windows\agent.py                     # config.json: "mode": "server" | "passive"
    daemons\windows\run-server.cmd                     # Windows default: server mode on :8910 (log: data\pylon-daemon.log)

Windows server mode: the daemon starts the server first, then its tray icon
points at it ("Open pylon-server", "Sign in", "WebSSH", "Server status"). The
tray helper's sidecar files and its single-instance sweep are scoped to the
install (`PYLON2_TRAY_PORT_FILE`, same framework URL), so a pylon-server tray
and an ai-pylon2 tray coexist on one desktop. The server runs in a Job Object
that kills its whole tree when the daemon exits (Linux: PR_SET_PDEATHSIG).

Local patches to the copied Windows caps: `caps/system_tray.py` (port file and
framework URL read when used) and `caps/_tray_host.py` (per-install files and
sweep).

Tests: `cd daemons && python3 -m unittest -q test_serve_mode test_tray_scope`.

Separate-repository server mode requires an explicit reviewed composed release root. Configure the daemon and server Python environments independently; passive mode needs only the agent dependencies.
