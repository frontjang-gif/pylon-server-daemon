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
must be monitored. The browser does not inherit Windows sessions. It now supports a separate human-operated viewer and optional named profiles as described below. No automated Google password entry, cookie extraction or consent automation is added.

## Interactive viewer and named profiles
The Linux capability now exposes explicit human viewer sessions. Open the
CDP Inspector's Interactive Linux browser panel, select the endpoint, and
enter temporary or a lowercase named profile (1–32 characters). The owner
must confirm. One browser/profile and one human viewer may run at a time.
General automation is refused while a human viewer is active; stop an idle
browser explicitly before switching profiles. Automation clients must pass
the same profile name when working with a named profile.

Named profiles live under ~/.local/share/pylon-server/cdp-profiles by default
(host-local PYLON_LINUX_CDP_PROFILE_ROOT override), in owner-only directories
with a per-profile lock. Profile data can include sensitive website session
state; do not place it in Git, shared folders or ordinary source backups.
The browser's own storage encryption is not guaranteed. Normal shutdown
attempts to flush browser state; server/daemon crashes or provider session
expiry may still require reauthentication.

Persistent startup requires the existing disk floor (at least 1 GiB) plus
256 MiB headroom; a watchdog stops the browser if the remaining reserve is
threatened. This is a free-space guard, not a hard quota. Temporary profiles
are cleaned on shutdown. The user operates the browser via private,
same-origin owner-authenticated POST requests and the existing AppUI remote
browser viewer. Frames and keyboard input are not stored by the viewer;
there are no public DevTools ports, cookies exported, provider credentials
captured or tool-facing viewer operations. The gateway owner cookie is
required; deploy-token-only requests do not authorize the viewer.

The initial viewer is hosted HTTPS only (trusted origin in Google Auth's
host-local configuration). It is separate from the fixed public-field OAuth
recipe. The current Google Auth setup recipe does not yet attach to a named
profile or the viewer's tab. Actual Google sign-in, MFA and consent remain
user-controlled; headless browser sign-in may be declined by the provider.
No attempt is made to evade such restrictions. Test persistence uses a
disposable profile and synthetic web page, not a real Google account.
