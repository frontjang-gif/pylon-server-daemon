# Daemon repository rules

- Own endpoint agents, capabilities, supervision and OS service installation. Passive mode must run without server source. Server mode takes an explicit reviewed release path and Python executable.
- Keep agent credentials host-local. Preserve endpoint authentication, privilege boundaries and supervision; never embed secrets in source.
- Exact reviewed tree approval before commit/push; no automatic publication.
