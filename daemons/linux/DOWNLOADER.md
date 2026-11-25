# Public VM downloader

The downloader runs on the VM independently of the Windows framework and accepts anonymous requests at **http://104.154.100.135:8080**. It saves files under `/home/frontjang/Downloads/ai-pylon2/<job-id>/<filename>`.

## GET: submit a URL

```sh
curl --get 'http://104.154.100.135:8080/download' \
  --data-urlencode 'url=https://example.com/file.zip' \
  --data-urlencode 'filename=file.zip'
```

## POST: custom source headers

```sh
curl 'http://104.154.100.135:8080/download' \
  -H 'Content-Type: application/json' \
  --data '{"url":"https://example.com/file.zip","headers":{"User-Agent":"MyDownloader","Authorization":"Bearer SOURCE_TOKEN"},"filename":"file.zip"}'
```

On Windows PowerShell, use `curl.exe` to select curl rather than its PowerShell alias. Custom `headers` apply to the remote source request; they are not headers for authenticating to this public service. The public endpoint uses plain HTTP; use it without confidential source credentials unless you add HTTPS in front of it.

Both calls return **202 Accepted** with `{"ok":true,"job":{"id":"...","status":"queued",...}}`. Processing continues after the client disconnects.

## Check completion

```sh
curl 'http://104.154.100.135:8080/jobs/JOB_ID'
```

The job changes from `queued` to `running`, then `done` or `error`. The result contains `bytes`, `total_bytes` when known, `path`, `filename`, and `sha256`. Failure and interrupted jobs include an `error`. Status links use unguessable job IDs; there is no public directory listing or file-serving route.

POST options:

| Field | Meaning |
| --- | --- |
| `url` | Required HTTP or HTTPS source URL |
| `headers` | Optional object containing source request headers |
| `filename` | Optional plain filename; otherwise use Content-Disposition or URL name |
| `method` | Source request method: `GET` (default) or `POST` |
| `body` | Optional source request text body |
| `json` | Optional source JSON body; use instead of `body` |
| `max_bytes` | Maximum downloaded bytes, up to 1 GiB; default 1 GiB |
| `timeout` | Total download timeout, 1–3600 seconds; default 1800 |

Example source POST:

```json
{"url":"https://example.com/export","method":"POST","headers":{"X-API-Key":"SOURCE_KEY"},"json":{"format":"zip"},"filename":"export.zip"}
```

## Operation and limits

- Two concurrent downloads, 32 queued/running requests, 10 submissions per minute per client IP.
- Maximum 1 GiB per file; 2 GiB total completed/active data and 512 MiB free disk reserve. At capacity, the service rejects new work; an operator must remove old job folders to free space. Restart the service after manual cleanup so its in-memory history matches disk.
- Maximum 1000 retained job records. Completed files are never automatically deleted.
- Atomic file publication; failed partial files are removed. Duplicate filenames go into different job folders.
- HTTP(S) only. Private, loopback, link-local, cloud metadata and private DNS destinations are blocked, including redirects. Custom credentials are stripped on cross-origin redirects.
- Source URLs, header values and request bodies are not recorded in job metadata. Access logging is disabled so query URLs are not written by this service.
- Restarted downloads are marked `interrupted`; submit again to retry. Existing completed files remain.

```sh
systemctl --user status ai-pylon2-download
journalctl --user -u ai-pylon2-download -n 50 --no-pager
systemctl --user restart ai-pylon2-download
systemctl --user disable --now ai-pylon2-download
```

The service starts at VM boot as `frontjang` under systemd. Google Cloud firewall rule `ai-pylon-download-1a3029555b6b` permits TCP 8080 only on the VM carrying its matching target tag. The Linux daemon, SSH return tunnel and Windows framework remain separate from the public HTTP service.

For another VM, deployment tooling installs the service with the Linux daemon. `personal/googlevm.download.publish` (confirm-gated) creates the scoped firewall rule and preserves existing VM tags. Its dry run shows the planned rule and public URL.

## Through ai-pylon2

The app also offers `download.submit` and `download.status` as HTTP/in-process/AI operations using `cap:linux.download`, and a local GET/POST gateway:

`http://127.0.0.1:8900/apps/personal/googlevm/download/?url=...`

The op form requires `confirm:true` when submitting. The direct public VM endpoint requires no sign-in or confirmation field.
