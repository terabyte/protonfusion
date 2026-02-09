"""Manage filter backups: create, load, list, delete."""

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.models.backup_models import Backup, BackupMetadata
from src.models.filter_models import ProtonMailFilter
from src.utils.config import BACKUPS_DIR, TOOL_VERSION

logger = logging.getLogger(__name__)


class BackupManager:
    """Manages filter backup files."""

    def __init__(self, backups_dir: Optional[Path] = None):
        self.backups_dir = backups_dir or BACKUPS_DIR
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, filters: List[ProtonMailFilter], account_email: str = "") -> Backup:
        """Create a new backup from a list of filters."""
        now = datetime.now()

        enabled_count = sum(1 for f in filters if f.enabled)
        disabled_count = len(filters) - enabled_count

        metadata = BackupMetadata(
            filter_count=len(filters),
            enabled_count=enabled_count,
            disabled_count=disabled_count,
            account_email=account_email,
            tool_version=TOOL_VERSION,
        )

        backup = Backup(
            timestamp=now,
            metadata=metadata,
            filters=filters,
        )

        # Calculate checksum
        filters_json = json.dumps([f.model_dump() for f in filters], sort_keys=True, default=str)
        backup.checksum = "sha256:" + hashlib.sha256(filters_json.encode()).hexdigest()

        # Save to file
        filename = now.strftime("%Y-%m-%d_%H-%M-%S") + ".json"
        filepath = self.backups_dir / filename

        with open(filepath, "w") as f:
            json.dump(backup.model_dump(), f, indent=2, default=str)

        # Update latest symlink
        latest_link = self.backups_dir / "latest.json"
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()
        latest_link.symlink_to(filename)

        logger.info("Backup created: %s (%d filters)", filepath, len(filters))
        return backup

    def load_backup(self, identifier: str = "latest") -> Backup:
        """Load a backup by timestamp or 'latest'."""
        if identifier == "latest":
            filepath = self.backups_dir / "latest.json"
            if not filepath.exists():
                raise FileNotFoundError("No latest backup found. Run 'backup' first.")
        else:
            # Try as filename
            filepath = Path(identifier)
            if not filepath.exists():
                # Try as timestamp in backups dir
                filepath = self.backups_dir / f"{identifier}.json"
            if not filepath.exists():
                raise FileNotFoundError(f"Backup not found: {identifier}")

        with open(filepath, "r") as f:
            data = json.load(f)

        backup = Backup.model_validate(data)
        logger.info("Loaded backup: %s (%d filters)", filepath, len(backup.filters))
        return backup

    def list_backups(self) -> List[dict]:
        """List all available backups with metadata."""
        backups = []

        for filepath in sorted(self.backups_dir.glob("*.json")):
            if filepath.name == "latest.json":
                continue
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                backups.append({
                    "filename": filepath.name,
                    "path": str(filepath),
                    "timestamp": data.get("timestamp", ""),
                    "filter_count": data.get("metadata", {}).get("filter_count", 0),
                    "enabled_count": data.get("metadata", {}).get("enabled_count", 0),
                    "disabled_count": data.get("metadata", {}).get("disabled_count", 0),
                    "size_bytes": filepath.stat().st_size,
                })
            except Exception as e:
                logger.warning("Failed to read backup %s: %s", filepath, e)

        return backups

    def verify_backup(self, backup: Backup) -> bool:
        """Verify backup integrity using checksum."""
        if not backup.checksum:
            logger.warning("Backup has no checksum")
            return False

        filters_json = json.dumps([f.model_dump() for f in backup.filters], sort_keys=True, default=str)
        computed = "sha256:" + hashlib.sha256(filters_json.encode()).hexdigest()

        is_valid = computed == backup.checksum
        if not is_valid:
            logger.error("Checksum mismatch! Expected %s, got %s", backup.checksum, computed)
        return is_valid

    def delete_backup(self, identifier: str) -> bool:
        """Delete a backup file."""
        filepath = self.backups_dir / f"{identifier}.json"
        if not filepath.exists():
            filepath = Path(identifier)

        if filepath.exists():
            filepath.unlink()
            logger.info("Deleted backup: %s", filepath)
            return True

        logger.warning("Backup not found: %s", identifier)
        return False
