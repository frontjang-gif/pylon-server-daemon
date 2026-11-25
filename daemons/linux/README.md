# Linux endpoint

`agent.py` speaks the same reverse JSON/WebSocket protocol as the Windows daemon; see `docs/PROTOCOL.md`. It registers as a separate endpoint, reconnects with backoff, sends heartbeats and runs blocking capability calls on bounded worker threads.

Capabilities are Linux-specific so existing desktop operations never round-robin onto a remote server:

| Capability | Methods |
| --- | --- |
| `cap:linux.system` | `status` (host, uptime, load, disk, process) |
| `cap:linux.terminal` | `run`, `create`, `read`, `write`, `resize`, `kill`, `list` |
| `cap:linux.files` | `list`, `read`, `write` |
| `cap:linux.download` | `submit`, `status` (URL download service) |
| `cap:_daemon` | `versions`, `list_caps` |

Terminal `run` returns separate stdout/stderr and a real exit code, limits output to 1 MiB per stream, and kills the process group on timeout (maximum five minutes). Interactive terminals use native POSIX PTYs with controlling terminals. File access is rooted at the daemon user's home by default, bounded to 1 MiB per file and rejects resolved paths outside the root. Set `PYLON2_FILE_ROOT` to restrict it further. Files are published atomically. The daemon runs with its service user's permissions.

## Deploy through an existing SSH host

From the repo root:

```powershell
py -3 dev/apps/personal/googlevm/deploy_linux.py --host <saved-ssh-alias> --endpoint <unique-linux-endpoint-id>
```

This verifies the uploaded archive checksum, installs into `~/.local/share/ai-pylon2/linux-daemon`, creates an isolated venv, and enables `ai-pylon2-linux.service` in the user's systemd instance. It enables linger so the service starts at boot without an SSH login. Debian hosts missing venv need passwordless sudo to install `python3-venv`; the daemon itself remains unprivileged and has `NoNewPrivileges=true`. Deployment uses the shared auth_ssh app and generic work_runner jobs, not another SSH stack.

Deployment also installs `ai-pylon2-download.service`, an independent anonymous HTTP downloader listening on TCP 8080. Its public firewall rule is managed separately by `personal/googlevm.download.publish`. See [DOWNLOADER.md](DOWNLOADER.md) for GET/POST examples, saved-file paths and operating limits.

Environment:

- `PYLON2_ENDPOINT_ID`: unique endpoint ID; default `linux-<hostname>`.
- `PYLON2_ENDPOINT_SCOPE`: `personal` (the Google VM deploy sets it) registers the endpoint as RESERVED - the framework
  routes a cap call to it only with an explicit endpoint hint, whatever its id; `shared` or unset = a normal endpoint.
- `PYLON2_FRAMEWORK_URL`: WebSocket URL, default `ws://127.0.0.1:18900/agent` for the private bridge.
- `PYLON2_FILE_ROOT`: file capability root; default home directory.

For a directly reachable framework, set a trusted private `ws://` URL or authenticated network/TLS setup before deploying. This installation uses a private SSH tunnel and does not expose the framework or agent port publicly.

## Private bridge for a Windows-hosted framework

`daemons/ssh_bridge.py --config <runtime-json>` keeps `platform/auth_ssh.reverse_open` active, restoring it after framework/network interruptions. It uses the existing saved SSH host and key; it holds no credentials. Example config:

```json
{"host":"saved-vm-alias","remote_port":18900,"local_port":8900,"guard_port":48991}
```

The remote listener binds only `127.0.0.1:18900`. A Windows Task Scheduler logon task runs the bridge with `pythonw.exe`, restarts on failure and allows only one task instance; a guard prevents duplicate manual launches. Runtime config and rotating logs live under `data/daemons/ssh_bridges/`. The daemon reconnects independently if its tunnel temporarily disappears. The Windows framework and bridge must be running to reach this endpoint. If the VM's public IP changes, update its saved SSH alias through Google VM's SSH action.

Install or repair the Windows bridge task with `dev/apps/personal/googlevm/install_bridge.ps1 -HostAlias <saved-alias> -EndpointId <linux-endpoint-id>`. To stop its autostart, disable the `ai-pylon2-googlevm-bridge` task in Task Scheduler (or use `Disable-ScheduledTask`).

## Operate and verify

```sh
systemctl --user status ai-pylon2-linux
journalctl --user -u ai-pylon2-linux -n 50 --no-pager
systemctl --user restart ai-pylon2-linux
systemctl --user disable --now ai-pylon2-linux
```

The framework's `/v1/endpoints` and `/v1/endpoints/caps` show registration and methods. Google VM exposes `daemon.status`, confirm-gated `daemon.run`, and read-only `daemon.files` as HTTP, in-process and AI tools. Its page has **Daemon status**. Defaults are recorded in `data/apps/personal/googlevm/daemon.json`. All operations target the configured Linux endpoint explicitly; existing Terminal SSH links continue to work independently.

Hermetic tests live in `dev/daemons/linux/test_linux.py`. Run them on Linux with `PYLON_TEST_DAEMON=<deployed-path> <venv>/bin/python <test-path> -v`; PTY/process tests are skipped on Windows.

## Endpoint compute capability
The daemon discovers caps/workload.py (cap:system.workload), using shared daemons/workload_runtime.py. The standalone installation includes workload_runtime.py beside agent.py. The endpoint owns a local receipt and job working directory under ~/.pylon2/endpoint-workloads; one concurrent compute, bounded 16 KiB output tails, up to one hour, 512 MiB admission reserve, 256 job records. No automatic deletion of output directories. Update both shim and shared module together. Windows uses the same shared runtime; Windows execution/descendant cleanup needs native acceptance.
