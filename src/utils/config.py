"""Configuration management for ProtonMail Filter Tool."""

import os
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass


# Project paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
BACKUPS_DIR = PROJECT_ROOT / "backups"
OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_CREDENTIALS_FILE = PROJECT_ROOT / ".credentials"

# Tool info
TOOL_VERSION = "0.1.0"

# ProtonMail URLs
PROTONMAIL_LOGIN_URL = "https://account.proton.me/login"
PROTONMAIL_SETTINGS_FILTERS_URL = "https://mail.proton.me/u/0/settings/filters"
PROTONMAIL_SIEVE_URL = "https://mail.proton.me/u/0/settings/filters#sieve"

# Timeouts
LOGIN_TIMEOUT_MS = 120000  # 2 minutes for manual login
PAGE_LOAD_TIMEOUT_MS = 60000
ELEMENT_TIMEOUT_MS = 10000

# Ensure directories exist
BACKUPS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


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
