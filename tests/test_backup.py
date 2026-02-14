"""Tests for backup manager."""

import pytest
import json
import hashlib
import time
from datetime import datetime
from pathlib import Path

from src.backup.backup_manager import BackupManager
from src.models.backup_models import Backup, BackupMetadata, ArchiveEntry, Archive
from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, FilterStatus,
    ConditionType, Operator, ActionType,
)


class TestBackupManager:
    """Test BackupManager class."""

    def test_init_creates_directory(self, tmp_path):
        """Test that BackupManager creates snapshots directory."""
        snapshots_dir = tmp_path / "snapshots"
        assert not snapshots_dir.exists()

        manager = BackupManager(snapshots_dir)

        assert snapshots_dir.exists()
        assert snapshots_dir.is_dir()

    def test_init_uses_existing_directory(self, temp_snapshots_dir):
        """Test that BackupManager works with existing directory."""
        manager = BackupManager(temp_snapshots_dir)
        assert manager.snapshots_dir == temp_snapshots_dir

    def test_create_backup_basic(self, temp_snapshots_dir, sample_filters_list):
        """Test creating a basic backup."""
        manager = BackupManager(temp_snapshots_dir)

        backup = manager.create_backup(sample_filters_list, "test@proton.me")

        assert isinstance(backup, Backup)
        assert len(backup.filters) == 3
        assert backup.metadata.filter_count == 3
        assert backup.metadata.account_email == "test@proton.me"

    def test_create_backup_counts_enabled_disabled(self, temp_snapshots_dir):
        """Test that backup correctly counts enabled/disabled filters."""
        manager = BackupManager(temp_snapshots_dir)
        filters = [
            ProtonMailFilter(name="Enabled 1", enabled=True),
            ProtonMailFilter(name="Enabled 2", enabled=True),
            ProtonMailFilter(name="Disabled 1", enabled=False),
        ]

        backup = manager.create_backup(filters)

        assert backup.metadata.filter_count == 3
        assert backup.metadata.enabled_count == 2
        assert backup.metadata.disabled_count == 1

    def test_create_backup_generates_checksum(self, temp_snapshots_dir, sample_filters_list):
        """Test that backup generates a checksum."""
        manager = BackupManager(temp_snapshots_dir)

        backup = manager.create_backup(sample_filters_list)

        assert backup.checksum.startswith("sha256:")
        assert len(backup.checksum) > 7  # More than just the prefix

    def test_create_backup_saves_file(self, temp_snapshots_dir, sample_filters_list):
        """Test that backup is saved to a snapshot subdirectory."""
        manager = BackupManager(temp_snapshots_dir)

        backup = manager.create_backup(sample_filters_list)

        # Check that a snapshot subdirectory was created with backup.json
        subdirs = [d for d in temp_snapshots_dir.iterdir() if d.is_dir() and d.name != "latest"]
        assert len(subdirs) == 1
        assert (subdirs[0] / "backup.json").exists()

    def test_create_backup_creates_latest_symlink(self, temp_snapshots_dir, sample_filters_list):
        """Test that backup creates/updates latest symlink."""
        manager = BackupManager(temp_snapshots_dir)

        backup = manager.create_backup(sample_filters_list)

        latest_link = temp_snapshots_dir / "latest"
        assert latest_link.exists()
        assert latest_link.is_symlink()

    def test_create_backup_updates_latest_symlink(self, temp_snapshots_dir, sample_filters_list):
        """Test that creating multiple backups updates the latest symlink."""
        import time
        manager = BackupManager(temp_snapshots_dir)

        backup1 = manager.create_backup([sample_filters_list[0]])
        time.sleep(1)
        backup2 = manager.create_backup(sample_filters_list)

        # Latest should point to the second backup
        latest_link = temp_snapshots_dir / "latest"
        assert latest_link.exists()
        assert latest_link.is_symlink()

    def test_load_backup_latest(self, temp_snapshots_dir, sample_filters_list):
        """Test loading the latest backup."""
        manager = BackupManager(temp_snapshots_dir)
        created = manager.create_backup(sample_filters_list, "test@proton.me")

        loaded = manager.load_backup("latest")

        assert len(loaded.filters) == 3
        assert loaded.metadata.account_email == "test@proton.me"

    def test_load_backup_by_dirname(self, temp_snapshots_dir, sample_filters_list):
        """Test loading backup by snapshot directory name."""
        manager = BackupManager(temp_snapshots_dir)
        backup = manager.create_backup(sample_filters_list)

        # Get the snapshot dirname
        subdirs = [d for d in temp_snapshots_dir.iterdir() if d.is_dir() and d.name != "latest"]
        assert len(subdirs) == 1
        dirname = subdirs[0].name

        loaded = manager.load_backup(dirname)

        assert len(loaded.filters) == 3

    def test_load_backup_not_found(self, temp_snapshots_dir):
        """Test loading non-existent backup raises error."""
        manager = BackupManager(temp_snapshots_dir)

        with pytest.raises(FileNotFoundError):
            manager.load_backup("nonexistent")

    def test_load_backup_no_latest(self, temp_snapshots_dir):
        """Test loading 'latest' when no backup exists raises error."""
        manager = BackupManager(temp_snapshots_dir)

        with pytest.raises(FileNotFoundError, match="No latest snapshot found"):
            manager.load_backup("latest")

    def test_list_backups_empty(self, temp_snapshots_dir):
        """Test listing backups when none exist."""
        manager = BackupManager(temp_snapshots_dir)

        backups = manager.list_backups()

        assert backups == []

    def test_list_backups_single(self, temp_snapshots_dir, sample_filters_list):
        """Test listing a single backup."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list, "test@proton.me")

        backups = manager.list_backups()

        assert len(backups) == 1
        assert backups[0]["filter_count"] == 3
        assert "timestamp" in backups[0]
        assert "snapshot" in backups[0]

    def test_list_backups_multiple(self, temp_snapshots_dir, sample_filters_list):
        """Test listing multiple backups."""
        import time
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup([sample_filters_list[0]])
        time.sleep(1)  # Ensure different timestamp
        manager.create_backup(sample_filters_list)

        backups = manager.list_backups()

        assert len(backups) == 2

    def test_list_backups_excludes_latest_symlink(self, temp_snapshots_dir, sample_filters_list):
        """Test that list_backups excludes latest symlink."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)

        backups = manager.list_backups()

        # Should have 1 backup, not 2 (backup + latest)
        assert len(backups) == 1
        assert backups[0]["snapshot"] != "latest"

    def test_list_backups_includes_metadata(self, temp_snapshots_dir, sample_filters_list):
        """Test that listed backups include metadata."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list, "test@proton.me")

        backups = manager.list_backups()

        assert len(backups) == 1
        b = backups[0]
        assert "snapshot" in b
        assert "path" in b
        assert "timestamp" in b
        assert "filter_count" in b
        assert "enabled_count" in b
        assert "disabled_count" in b
        assert "size_bytes" in b

    def test_verify_backup_valid(self, temp_snapshots_dir, sample_filters_list):
        """Test verifying a valid backup."""
        manager = BackupManager(temp_snapshots_dir)
        backup = manager.create_backup(sample_filters_list)

        is_valid = manager.verify_backup(backup)

        assert is_valid is True

    def test_verify_backup_no_checksum(self, temp_snapshots_dir):
        """Test verifying backup without checksum."""
        manager = BackupManager(temp_snapshots_dir)
        backup = Backup(filters=[])
        backup.checksum = ""

        is_valid = manager.verify_backup(backup)

        assert is_valid is False

    def test_verify_backup_invalid_checksum(self, temp_snapshots_dir, sample_filters_list):
        """Test verifying backup with invalid checksum."""
        manager = BackupManager(temp_snapshots_dir)
        backup = manager.create_backup(sample_filters_list)
        backup.checksum = "sha256:invalid"

        is_valid = manager.verify_backup(backup)

        assert is_valid is False

    def test_verify_backup_tampered_data(self, temp_snapshots_dir, sample_filters_list):
        """Test verifying backup with tampered data."""
        manager = BackupManager(temp_snapshots_dir)
        backup = manager.create_backup(sample_filters_list)

        # Tamper with the data
        backup.filters.append(ProtonMailFilter(name="Tampered"))

        is_valid = manager.verify_backup(backup)

        assert is_valid is False

    def test_delete_backup_success(self, temp_snapshots_dir, sample_filters_list):
        """Test deleting a snapshot."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)

        # Get the snapshot dirname
        subdirs = [d for d in temp_snapshots_dir.iterdir() if d.is_dir() and d.name != "latest"]
        dirname = subdirs[0].name

        result = manager.delete_backup(dirname)

        assert result is True
        assert not (temp_snapshots_dir / dirname).exists()

    def test_delete_backup_not_found(self, temp_snapshots_dir):
        """Test deleting non-existent snapshot."""
        manager = BackupManager(temp_snapshots_dir)

        result = manager.delete_backup("nonexistent")

        assert result is False

    def test_backup_dirname_format(self, temp_snapshots_dir, sample_filters_list):
        """Test that snapshot dirname uses correct datetime format."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)

        subdirs = [d for d in temp_snapshots_dir.iterdir() if d.is_dir() and d.name != "latest"]
        assert len(subdirs) == 1

        dirname = subdirs[0].name
        # Should match format: YYYY-MM-DD_HH-MM-SS
        assert dirname.count("-") == 4  # 2 in date, 2 in time
        assert "_" in dirname

    def test_backup_contains_version(self, temp_snapshots_dir, sample_filters_list):
        """Test that saved backup contains version."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)

        # Load the backup file directly
        subdirs = [d for d in temp_snapshots_dir.iterdir() if d.is_dir() and d.name != "latest"]
        with open(subdirs[0] / "backup.json") as f:
            data = json.load(f)

        assert "version" in data
        assert data["version"] == "1.0"

    def test_backup_contains_timestamp(self, temp_snapshots_dir, sample_filters_list):
        """Test that saved backup contains timestamp."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)

        subdirs = [d for d in temp_snapshots_dir.iterdir() if d.is_dir() and d.name != "latest"]
        with open(subdirs[0] / "backup.json") as f:
            data = json.load(f)

        assert "timestamp" in data

    def test_empty_backup(self, temp_snapshots_dir):
        """Test creating a backup with no filters."""
        manager = BackupManager(temp_snapshots_dir)

        backup = manager.create_backup([])

        assert backup.metadata.filter_count == 0
        assert backup.metadata.enabled_count == 0
        assert backup.metadata.disabled_count == 0
        assert len(backup.filters) == 0


class TestManifest:
    """Test manifest methods."""

    def test_write_manifest(self, temp_snapshots_dir, sample_filters_list):
        """Test writing a manifest."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        manager.write_manifest(snapshot_dir, sample_filters_list, "consolidated.sieve")

        manifest_path = snapshot_dir / "manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["filter_count"] == 3
        assert data["sieve_file"] == "consolidated.sieve"
        assert data["synced_at"] is None
        assert len(data["filter_hashes"]) > 0
        assert len(data["filter_names"]) > 0

    def test_load_manifest(self, temp_snapshots_dir, sample_filters_list):
        """Test loading a manifest."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")
        manager.write_manifest(snapshot_dir, sample_filters_list, "consolidated.sieve")

        manifest = manager.load_manifest(snapshot_dir)

        assert manifest is not None
        assert manifest["filter_count"] == 3
        assert manifest["synced_at"] is None

    def test_load_manifest_missing(self, temp_snapshots_dir, sample_filters_list):
        """Test loading a manifest when none exists."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        manifest = manager.load_manifest(snapshot_dir)

        assert manifest is None

    def test_promote_manifest(self, temp_snapshots_dir, sample_filters_list):
        """Test promoting a manifest (setting synced_at)."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")
        manager.write_manifest(snapshot_dir, sample_filters_list, "consolidated.sieve")

        result = manager.promote_manifest(snapshot_dir)

        assert result is True
        manifest = manager.load_manifest(snapshot_dir)
        assert manifest["synced_at"] is not None

    def test_promote_manifest_missing(self, temp_snapshots_dir, sample_filters_list):
        """Test promoting when no manifest exists."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        result = manager.promote_manifest(snapshot_dir)

        assert result is False

    def test_load_synced_hashes(self, temp_snapshots_dir, sample_filters_list):
        """Test loading synced hashes from latest synced manifest."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")
        manager.write_manifest(snapshot_dir, sample_filters_list, "consolidated.sieve")
        manager.promote_manifest(snapshot_dir)

        hashes = manager.load_synced_hashes()

        assert hashes is not None
        assert len(hashes) > 0

    def test_load_synced_hashes_none_synced(self, temp_snapshots_dir, sample_filters_list):
        """Test loading synced hashes when no manifest is synced."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")
        manager.write_manifest(snapshot_dir, sample_filters_list, "consolidated.sieve")
        # Don't promote

        hashes = manager.load_synced_hashes()

        assert hashes is None

    def test_load_synced_hashes_empty(self, temp_snapshots_dir):
        """Test loading synced hashes when no snapshots exist."""
        manager = BackupManager(temp_snapshots_dir)

        hashes = manager.load_synced_hashes()

        assert hashes is None


class TestArchiveIO:
    """Test archive read/write methods."""

    def _make_entry(self, name, status=FilterStatus.ARCHIVED):
        """Helper to create an archive entry."""
        f = ProtonMailFilter(
            name=name,
            status=status,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value=f"{name}@test.com")],
            actions=[FilterAction(type=ActionType.DELETE)],
        )
        return ArchiveEntry(filter=f, archived_at="2025-01-01T00:00:00Z", source_snapshot="snap1")

    def test_write_archive(self, temp_snapshots_dir, sample_filters_list):
        """Test writing an archive file."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        entries = [self._make_entry("Filter1"), self._make_entry("Filter2")]
        manager.write_archive(snapshot_dir, entries)

        archive_path = snapshot_dir / "archive.json"
        assert archive_path.exists()
        data = json.loads(archive_path.read_text())
        assert data["version"] == "1.0"
        assert len(data["entries"]) == 2

    def test_load_archive_exists(self, temp_snapshots_dir, sample_filters_list):
        """Test loading an existing archive."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        entries = [self._make_entry("Filter1")]
        manager.write_archive(snapshot_dir, entries)

        loaded = manager.load_archive(snapshot_dir)
        assert len(loaded) == 1
        assert loaded[0].filter.name == "Filter1"
        assert loaded[0].filter.status == FilterStatus.ARCHIVED

    def test_load_archive_missing(self, temp_snapshots_dir, sample_filters_list):
        """Test loading archive when none exists returns empty list."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        loaded = manager.load_archive(snapshot_dir)
        assert loaded == []

    def test_carry_forward_archive_with_previous(self, temp_snapshots_dir, sample_filters_list):
        """Test carrying forward archive from previous snapshot."""
        manager = BackupManager(temp_snapshots_dir)

        # Create first backup with archive
        manager.create_backup(sample_filters_list)
        first_dir = manager.snapshot_dir_for("latest")
        entries = [self._make_entry("Carried")]
        manager.write_archive(first_dir, entries)

        # Create second backup — archive should carry forward
        time.sleep(1)
        manager.create_backup(sample_filters_list)
        second_dir = manager.snapshot_dir_for("latest")

        # Archive should have been carried forward
        loaded = manager.load_archive(second_dir)
        assert len(loaded) == 1
        assert loaded[0].filter.name == "Carried"

    def test_carry_forward_archive_without_previous(self, temp_snapshots_dir, sample_filters_list):
        """Test carry forward when no previous archive exists."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)

        # No archive in first snapshot — carry_forward should return empty
        target = temp_snapshots_dir / "test-target"
        target.mkdir()
        carried = manager.carry_forward_archive(target)
        assert carried == []

    def test_carry_forward_does_not_self_copy(self, temp_snapshots_dir, sample_filters_list):
        """Test that carry forward doesn't copy from self."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        entries = [self._make_entry("Test")]
        manager.write_archive(snapshot_dir, entries)

        # carry_forward from latest to itself should return empty
        carried = manager.carry_forward_archive(snapshot_dir)
        assert carried == []

    def test_create_backup_carries_forward_archive(self, temp_snapshots_dir, sample_filters_list):
        """Test that create_backup automatically carries forward archive."""
        manager = BackupManager(temp_snapshots_dir)

        # First backup with archive
        manager.create_backup(sample_filters_list)
        first_dir = manager.snapshot_dir_for("latest")
        manager.write_archive(first_dir, [self._make_entry("AutoCarry")])

        # Second backup
        time.sleep(1)
        manager.create_backup(sample_filters_list)
        second_dir = manager.snapshot_dir_for("latest")

        loaded = manager.load_archive(second_dir)
        assert len(loaded) == 1
        assert loaded[0].filter.name == "AutoCarry"

    def test_write_archive_overwrites(self, temp_snapshots_dir, sample_filters_list):
        """Test that writing archive overwrites existing."""
        manager = BackupManager(temp_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        manager.write_archive(snapshot_dir, [self._make_entry("First")])
        manager.write_archive(snapshot_dir, [self._make_entry("Second")])

        loaded = manager.load_archive(snapshot_dir)
        assert len(loaded) == 1
        assert loaded[0].filter.name == "Second"
