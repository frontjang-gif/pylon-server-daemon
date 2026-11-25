"""Read the VM-only owner credential from a private file."""
from pathlib import Path
import stat


def load_owner_password(config, root):
    path = Path(root) / config.get('owner_credentials_file', 'state/owner-password')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('Owner credential must have mode 0600')
    password = path.read_text(encoding='utf-8').strip()
    if len(password) < 32:
        raise ValueError('Owner credential must contain at least 32 characters')
    return password
