"""Manage filter backups: create, load, list, delete."""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.models.backup_models import Backup, BackupMetadata, Archive, ArchiveEntry
from src.models.filter_models import ProtonMailFilter
from src.utils.config import SNAPSHOTS_DIR, TOOL_VERSION

logger = logging.getLogger(__name__)


class BackupManager:
    """Manages filter backups inside timestamped snapshot directories."""

    def __init__(self, snapshots_dir: Optional[Path] = None):
        self.snapshots_dir = snapshots_dir or SNAPSHOTS_DIR
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(
        self, filters: List[ProtonMailFilter], account_email: str = "", sieve_script: str = "",
    ) -> Backup:
        """Create a new backup inside a timestamped snapshot directory."""
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
            sieve_script=sieve_script,
        )

        # Calculate checksum (includes sieve_script for integrity)
        checksum_data = {
            "filters": [f.model_dump() for f in filters],
            "sieve_script": sieve_script,
        }
        checksum_json = json.dumps(checksum_data, sort_keys=True, default=str)
        backup.checksum = "sha256:" + hashlib.sha256(checksum_json.encode()).hexdigest()

        # Create snapshot subdirectory
        dirname = now.strftime("%Y-%m-%d_%H-%M-%S")
        snapshot_dir = self.snapshots_dir / dirname
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Save backup.json inside the snapshot dir
        filepath = snapshot_dir / "backup.json"
        with open(filepath, "w") as f:
            json.dump(backup.model_dump(), f, indent=2, default=str)

        # Carry forward archive from previous snapshot before updating symlink
        self.carry_forward_archive(snapshot_dir)

        # Update latest symlink at snapshots/latest -> dirname
        latest_link = self.snapshots_dir / "latest"
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()
        latest_link.symlink_to(dirname)

        logger.info("Backup created: %s (%d filters)", snapshot_dir, len(filters))
        return backup

    def snapshot_dir_for(self, identifier: str = "latest") -> Path:
        """Resolve a snapshot identifier to its directory path."""
        if identifier == "latest":
            latest_link = self.snapshots_dir / "latest"
            if not latest_link.exists():
                raise FileNotFoundError("No latest snapshot found. Run 'backup' first.")
            return latest_link.resolve()
        # Try as a timestamp dirname
        candidate = self.snapshots_dir / identifier
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"Snapshot not found: {identifier}")

    def load_backup(self, identifier: str = "latest") -> Backup:
        """Load a backup by timestamp or 'latest'."""
        snapshot_dir = self.snapshot_dir_for(identifier)
        filepath = snapshot_dir / "backup.json"
        if not filepath.exists():
            raise FileNotFoundError(f"No backup.json in snapshot: {snapshot_dir}")

        with open(filepath, "r") as f:
            data = json.load(f)

        backup = Backup.model_validate(data)
        logger.info("Loaded backup: %s (%d filters)", filepath, len(backup.filters))
        return backup

    def list_backups(self) -> List[dict]:
        """List all available snapshots with metadata."""
        backups = []

        for entry in sorted(self.snapshots_dir.iterdir()):
            if entry.name == "latest" or not entry.is_dir():
                continue
            backup_file = entry / "backup.json"
            if not backup_file.exists():
                continue
            try:
                with open(backup_file, "r") as f:
                    data = json.load(f)
                backups.append({
                    "snapshot": entry.name,
                    "path": str(entry),
                    "timestamp": data.get("timestamp", ""),
                    "filter_count": data.get("metadata", {}).get("filter_count", 0),
                    "enabled_count": data.get("metadata", {}).get("enabled_count", 0),
                    "disabled_count": data.get("metadata", {}).get("disabled_count", 0),
                    "size_bytes": backup_file.stat().st_size,
                })
            except Exception as e:
                logger.warning("Failed to read snapshot %s: %s", entry, e)

        return backups

    def verify_backup(self, backup: Backup) -> bool:
        """Verify backup integrity using checksum."""
        if not backup.checksum:
            logger.warning("Backup has no checksum")
            return False

        checksum_data = {
            "filters": [f.model_dump() for f in backup.filters],
            "sieve_script": backup.sieve_script,
        }
        checksum_json = json.dumps(checksum_data, sort_keys=True, default=str)
        computed = "sha256:" + hashlib.sha256(checksum_json.encode()).hexdigest()

        is_valid = computed == backup.checksum
        if not is_valid:
            logger.error("Checksum mismatch! Expected %s, got %s", backup.checksum, computed)
        return is_valid

    def delete_backup(self, identifier: str) -> bool:
        """Delete a snapshot directory."""
        import shutil
        candidate = self.snapshots_dir / identifier
        if candidate.is_dir():
            shutil.rmtree(candidate)
            logger.info("Deleted snapshot: %s", candidate)
            return True
        logger.warning("Snapshot not found: %s", identifier)
        return False

    # --- Archive methods ---

    def write_archive(self, snapshot_dir: Path, entries: List[ArchiveEntry]):
        """Serialize archive entries to archive.json in the snapshot directory."""
        archive = Archive(entries=entries)
        archive_path = snapshot_dir / "archive.json"
        archive_path.write_text(json.dumps(archive.model_dump(), indent=2, default=str))
        logger.info("Archive written: %s (%d entries)", archive_path, len(entries))

    def load_archive(self, snapshot_dir: Path) -> List[ArchiveEntry]:
        """Load archive.json from a snapshot directory. Returns empty list if absent."""
        archive_path = snapshot_dir / "archive.json"
        if not archive_path.exists():
            return []
        data = json.loads(archive_path.read_text())
        archive = Archive.model_validate(data)
        return archive.entries

    def carry_forward_archive(self, target_dir: Path) -> List[ArchiveEntry]:
        """Copy archive.json from the latest symlink to target_dir.

        Returns the carried-forward entries (empty list if no previous archive).
        """
        latest_link = self.snapshots_dir / "latest"
        if not latest_link.exists() and not latest_link.is_symlink():
            return []
        try:
            prev_dir = latest_link.resolve()
        except OSError:
            return []
        if prev_dir == target_dir:
            # Don't carry forward from ourselves
            return []
        entries = self.load_archive(prev_dir)
        if entries:
            self.write_archive(target_dir, entries)
            logger.info("Archive carried forward: %d entries from %s", len(entries), prev_dir.name)
        return entries

    # --- Manifest methods ---

    def write_manifest(self, snapshot_dir: Path, filters: list, sieve_file: str):
        """Write manifest.json into a snapshot directory."""
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filter_hashes": sorted(set(f.content_hash for f in filters)),
            "filter_names": sorted(set(f.name for f in filters)),
            "filter_count": len(filters),
            "sieve_file": sieve_file,
            "synced_at": None,
        }
        manifest_path = snapshot_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        logger.info("Manifest written: %s (%d filters)", manifest_path, len(filters))

    def load_manifest(self, snapshot_dir: Path) -> Optional[dict]:
        """Load manifest.json from a snapshot directory."""
        manifest_path = snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text())

    def promote_manifest(self, snapshot_dir: Path) -> bool:
        """Mark a manifest as synced by setting synced_at."""
        manifest = self.load_manifest(snapshot_dir)
        if manifest is None:
            return False
        manifest["synced_at"] = datetime.now(timezone.utc).isoformat()
        manifest_path = snapshot_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        logger.info("Manifest promoted (synced): %s", manifest_path)
        return True

    def load_synced_hashes(self) -> Optional[set]:
        """Load filter content hashes from the latest synced manifest."""
        # Walk snapshots in reverse chronological order, find latest synced one
        for entry in sorted(self.snapshots_dir.iterdir(), reverse=True):
            if entry.name == "latest" or not entry.is_dir():
                continue
            manifest = self.load_manifest(entry)
            if manifest and manifest.get("synced_at"):
                return set(manifest.get("filter_hashes", []))
        return None
