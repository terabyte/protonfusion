"""Configuration management for ProtonFusion."""

import os
import re
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse


# Project paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_CREDENTIALS_FILE = PROJECT_ROOT / ".credentials"

# Snapshot directory (overridable via env var for test isolation)
_data_dir = os.environ.get("PROTONFUSION_DATA_DIR")
SNAPSHOTS_DIR = Path(_data_dir) if _data_dir else PROJECT_ROOT / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Tool info
TOOL_VERSION = "0.1.0"

# ProtonMail URLs
PROTONMAIL_LOGIN_URL = "https://account.proton.me/login"
MAIL_HOST = "mail.proton.me"
ACCOUNT_HOST = "account.proton.me"

# Proton addresses each signed-in account by a session slot in the path
# (mail.proton.me/u/<slot>/inbox). The slot is not always 0: a fresh login can
# land on /u/1/, so every app URL is built from the slot detected at login.
DEFAULT_ACCOUNT_SLOT = 0
INBOX_PATH = "inbox"
FILTERS_PATH = "mail/filters"
_SLOT_RE = re.compile(r"^/u/(\d+)(?:/|$)")


def proton_url(host: str, path: str, slot: int = DEFAULT_ACCOUNT_SLOT) -> str:
    """Build a Proton app URL for the given session slot.

    >>> proton_url(ACCOUNT_HOST, FILTERS_PATH, 1)
    'https://account.proton.me/u/1/mail/filters'
    """
    return f"https://{host}/u/{slot}/{path.lstrip('/')}"


def slot_from_url(url: str) -> Optional[int]:
    """Return the session slot in a mail/account.proton.me URL, or None."""
    parsed = urlparse(url or "")
    if parsed.hostname not in (MAIL_HOST, ACCOUNT_HOST):
        return None
    match = _SLOT_RE.match(parsed.path)
    return int(match.group(1)) if match else None


# Saved browser session (Playwright storage state: cookies + localStorage).
# Proton puts a Human Verification CAPTCHA in front of automated logins, so a
# human signs in once with `protonfusion login`, which saves the session here,
# and later commands reuse it until Proton expires it.
STORAGE_STATE_ENV = "PROTONFUSION_STORAGE_STATE"


def default_storage_state_path() -> Path:
    """$XDG_CONFIG_HOME/protonfusion/storage_state.json (~/.config if unset)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "protonfusion" / "storage_state.json"


def resolve_storage_state_path(cli_value: Optional[str] = None) -> Path:
    """Where the saved session lives: --state, else $PROTONFUSION_STORAGE_STATE, else the default."""
    chosen = cli_value or os.environ.get(STORAGE_STATE_ENV) or default_storage_state_path()
    return Path(chosen).expanduser()


# Timeouts
LOGIN_TIMEOUT_MS = 120000  # 2 minutes for manual login
PAGE_LOAD_TIMEOUT_MS = 60000
ELEMENT_TIMEOUT_MS = 10000


@dataclass
class Credentials:
    username: str
    password: str


def load_credentials(credentials_file: Optional[str] = None) -> Credentials:
    """Load credentials from file.

    File format:
        Username: <username>
        Password: <password>
    """
    file_path = Path(credentials_file) if credentials_file else DEFAULT_CREDENTIALS_FILE

    if not file_path.exists():
        raise FileNotFoundError(f"Credentials file not found: {file_path}")

    username = ""
    password = ""

    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Username:"):
                username = line.split(":", 1)[1].strip()
            elif line.startswith("Password:"):
                password = line.split(":", 1)[1].strip()

    if not username or not password:
        raise ValueError(f"Invalid credentials file format. Expected 'Username: ...' and 'Password: ...' lines.")

    return Credentials(username=username, password=password)
