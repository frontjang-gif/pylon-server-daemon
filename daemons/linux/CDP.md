# Linux CDP
The Linux endpoint advertises cap:system.cdp with the headless variant.
An installed Chromium/Chrome or cached Playwright Chromium headless shell is
selected automatically; PYLON_LINUX_CDP_BROWSER may name an absolute executable.
No browser is downloaded. The browser starts on demand with default sandbox
settings and an ephemeral private profile in /dev/shm when available.
DevTools listens on an automatically assigned loopback-only port. Never
publish this port. Browser traffic uses the endpoint's own network.

Methods: status, tabs, navigate, eval, close, stop (explicit confirm).
New navigation supports HTTP(S), and eval/close require an explicit tab ID.
Up to four page targets; bounded protocol responses and command deadlines.
Idle browser lifetime is approximately five minutes, or until daemon shutdown.
This is not a hardened separate browser VM or a hard memory quota: capacity
must be monitored. The browser does not inherit Windows sessions. It has no
interactive sign-in, screencast or persistent login profile in this release.
No Google passwords, cookie extraction or consent automation is added.
For signed-in Google Console setup, use a user-visible managed browser with
an existing authorized session. Headless Linux supports public-page tasks and
recognition of the sign-in gate.
