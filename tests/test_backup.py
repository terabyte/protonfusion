"""Tests for backup manager."""

import pytest
import json
import hashlib
from datetime import datetime
from pathlib import Path

from src.backup.backup_manager import BackupManager
from src.models.backup_models import Backup, BackupMetadata
from src.models.filter_models import ProtonMailFilter


class TestBackupManager:
    """Test BackupManager class."""

    def test_init_creates_directory(self, tmp_path):
        """Test that BackupManager creates backups directory."""
        backups_dir = tmp_path / "backups"
        assert not backups_dir.exists()

        manager = BackupManager(backups_dir)

        assert backups_dir.exists()
        assert backups_dir.is_dir()

    def test_init_uses_existing_directory(self, temp_backups_dir):
        """Test that BackupManager works with existing directory."""
        manager = BackupManager(temp_backups_dir)
        assert manager.backups_dir == temp_backups_dir

    def test_create_backup_basic(self, temp_backups_dir, sample_filters_list):
        """Test creating a basic backup."""
        manager = BackupManager(temp_backups_dir)

        backup = manager.create_backup(sample_filters_list, "test@proton.me")

        assert isinstance(backup, Backup)
        assert len(backup.filters) == 3
        assert backup.metadata.filter_count == 3
        assert backup.metadata.account_email == "test@proton.me"

    def test_create_backup_counts_enabled_disabled(self, temp_backups_dir):
        """Test that backup correctly counts enabled/disabled filters."""
        manager = BackupManager(temp_backups_dir)
        filters = [
            ProtonMailFilter(name="Enabled 1", enabled=True),
            ProtonMailFilter(name="Enabled 2", enabled=True),
            ProtonMailFilter(name="Disabled 1", enabled=False),
        ]

        backup = manager.create_backup(filters)

        assert backup.metadata.filter_count == 3
        assert backup.metadata.enabled_count == 2
        assert backup.metadata.disabled_count == 1

    def test_create_backup_generates_checksum(self, temp_backups_dir, sample_filters_list):
        """Test that backup generates a checksum."""
        manager = BackupManager(temp_backups_dir)

        backup = manager.create_backup(sample_filters_list)

        assert backup.checksum.startswith("sha256:")
        assert len(backup.checksum) > 7  # More than just the prefix

    def test_create_backup_saves_file(self, temp_backups_dir, sample_filters_list):
        """Test that backup is saved to a file."""
        manager = BackupManager(temp_backups_dir)

        backup = manager.create_backup(sample_filters_list)

        # Check that a JSON file was created
        json_files = list(temp_backups_dir.glob("*.json"))
        assert len(json_files) >= 1  # At least the backup file (possibly latest.json symlink too)

    def test_create_backup_creates_latest_symlink(self, temp_backups_dir, sample_filters_list):
        """Test that backup creates/updates latest.json symlink."""
        manager = BackupManager(temp_backups_dir)

        backup = manager.create_backup(sample_filters_list)

        latest_link = temp_backups_dir / "latest.json"
        assert latest_link.exists()
        assert latest_link.is_symlink() or latest_link.is_file()

    def test_create_backup_updates_latest_symlink(self, temp_backups_dir, sample_filters_list):
        """Test that creating multiple backups updates the latest symlink."""
        manager = BackupManager(temp_backups_dir)

        backup1 = manager.create_backup([sample_filters_list[0]])
        backup2 = manager.create_backup(sample_filters_list)

        # Latest should point to the second backup
        latest_link = temp_backups_dir / "latest.json"
        assert latest_link.exists()

    def test_load_backup_latest(self, temp_backups_dir, sample_filters_list):
        """Test loading the latest backup."""
        manager = BackupManager(temp_backups_dir)
        created = manager.create_backup(sample_filters_list, "test@proton.me")

        loaded = manager.load_backup("latest")

        assert len(loaded.filters) == 3
        assert loaded.metadata.account_email == "test@proton.me"

    def test_load_backup_by_filename(self, temp_backups_dir, sample_filters_list):
        """Test loading backup by filename."""
        manager = BackupManager(temp_backups_dir)
        backup = manager.create_backup(sample_filters_list)

        # Get the filename
        files = [f for f in temp_backups_dir.glob("*.json") if f.name != "latest.json"]
        assert len(files) == 1
        filename = files[0].name

        loaded = manager.load_backup(filename.replace(".json", ""))

        assert len(loaded.filters) == 3

    def test_load_backup_not_found(self, temp_backups_dir):
        """Test loading non-existent backup raises error."""
        manager = BackupManager(temp_backups_dir)

        with pytest.raises(FileNotFoundError):
            manager.load_backup("nonexistent")

    def test_load_backup_no_latest(self, temp_backups_dir):
        """Test loading 'latest' when no backup exists raises error."""
        manager = BackupManager(temp_backups_dir)

        with pytest.raises(FileNotFoundError, match="No latest backup found"):
            manager.load_backup("latest")

    def test_list_backups_empty(self, temp_backups_dir):
        """Test listing backups when none exist."""
        manager = BackupManager(temp_backups_dir)

        backups = manager.list_backups()

        assert backups == []

    def test_list_backups_single(self, temp_backups_dir, sample_filters_list):
        """Test listing a single backup."""
        manager = BackupManager(temp_backups_dir)
        manager.create_backup(sample_filters_list, "test@proton.me")

        backups = manager.list_backups()

        assert len(backups) == 1
        assert backups[0]["filter_count"] == 3
        assert "timestamp" in backups[0]
        assert "filename" in backups[0]

    def test_list_backups_multiple(self, temp_backups_dir, sample_filters_list):
        """Test listing multiple backups."""
        import time
        manager = BackupManager(temp_backups_dir)
        manager.create_backup([sample_filters_list[0]])
        time.sleep(1)  # Ensure different timestamp
        manager.create_backup(sample_filters_list)

        backups = manager.list_backups()

        assert len(backups) == 2

    def test_list_backups_excludes_latest_symlink(self, temp_backups_dir, sample_filters_list):
        """Test that list_backups excludes latest.json symlink."""
        manager = BackupManager(temp_backups_dir)
        manager.create_backup(sample_filters_list)

        backups = manager.list_backups()

        # Should have 1 backup, not 2 (backup + latest.json)
        assert len(backups) == 1
        assert backups[0]["filename"] != "latest.json"

    def test_list_backups_includes_metadata(self, temp_backups_dir, sample_filters_list):
        """Test that listed backups include metadata."""
        manager = BackupManager(temp_backups_dir)
        manager.create_backup(sample_filters_list, "test@proton.me")

        backups = manager.list_backups()

        assert len(backups) == 1
        b = backups[0]
        assert "filename" in b
        assert "path" in b
        assert "timestamp" in b
        assert "filter_count" in b
        assert "enabled_count" in b
        assert "disabled_count" in b
        assert "size_bytes" in b

    def test_verify_backup_valid(self, temp_backups_dir, sample_filters_list):
        """Test verifying a valid backup."""
        manager = BackupManager(temp_backups_dir)
        backup = manager.create_backup(sample_filters_list)

        is_valid = manager.verify_backup(backup)

        assert is_valid is True

    def test_verify_backup_no_checksum(self, temp_backups_dir):
        """Test verifying backup without checksum."""
        manager = BackupManager(temp_backups_dir)
        backup = Backup(filters=[])
        backup.checksum = ""

        is_valid = manager.verify_backup(backup)

        assert is_valid is False

    def test_verify_backup_invalid_checksum(self, temp_backups_dir, sample_filters_list):
        """Test verifying backup with invalid checksum."""
        manager = BackupManager(temp_backups_dir)
        backup = manager.create_backup(sample_filters_list)
        backup.checksum = "sha256:invalid"

        is_valid = manager.verify_backup(backup)

        assert is_valid is False

    def test_verify_backup_tampered_data(self, temp_backups_dir, sample_filters_list):
        """Test verifying backup with tampered data."""
        manager = BackupManager(temp_backups_dir)
        backup = manager.create_backup(sample_filters_list)

        # Tamper with the data
        backup.filters.append(ProtonMailFilter(name="Tampered"))

        is_valid = manager.verify_backup(backup)

        assert is_valid is False

    def test_delete_backup_success(self, temp_backups_dir, sample_filters_list):
        """Test deleting a backup."""
        manager = BackupManager(temp_backups_dir)
        manager.create_backup(sample_filters_list)

        # Get the filename
        files = [f for f in temp_backups_dir.glob("*.json") if f.name != "latest.json"]
        filename = files[0].name.replace(".json", "")

        result = manager.delete_backup(filename)

        assert result is True
        # File should no longer exist
        assert not (temp_backups_dir / f"{filename}.json").exists()

    def test_delete_backup_not_found(self, temp_backups_dir):
        """Test deleting non-existent backup."""
        manager = BackupManager(temp_backups_dir)

        result = manager.delete_backup("nonexistent")

        assert result is False

    def test_backup_filename_format(self, temp_backups_dir, sample_filters_list):
        """Test that backup filename uses correct datetime format."""
        manager = BackupManager(temp_backups_dir)
        manager.create_backup(sample_filters_list)

        files = [f for f in temp_backups_dir.glob("*.json") if f.name != "latest.json"]
        assert len(files) == 1

        filename = files[0].name
        # Should match format: YYYY-MM-DD_HH-MM-SS.json
        assert filename.count("-") == 4  # 2 in date, 2 in time (date and time separated by underscore)
        assert filename.endswith(".json")

    def test_backup_contains_version(self, temp_backups_dir, sample_filters_list):
        """Test that saved backup contains version."""
        manager = BackupManager(temp_backups_dir)
        manager.create_backup(sample_filters_list)

        # Load the backup file directly
        files = [f for f in temp_backups_dir.glob("*.json") if f.name != "latest.json"]
        with open(files[0]) as f:
            data = json.load(f)

        assert "version" in data
        assert data["version"] == "1.0"

    def test_backup_contains_timestamp(self, temp_backups_dir, sample_filters_list):
        """Test that saved backup contains timestamp."""
        manager = BackupManager(temp_backups_dir)
        manager.create_backup(sample_filters_list)

        # Load the backup file directly
        files = [f for f in temp_backups_dir.glob("*.json") if f.name != "latest.json"]
        with open(files[0]) as f:
            data = json.load(f)

        assert "timestamp" in data

    def test_empty_backup(self, temp_backups_dir):
        """Test creating a backup with no filters."""
        manager = BackupManager(temp_backups_dir)

        backup = manager.create_backup([])

        assert backup.metadata.filter_count == 0
        assert backup.metadata.enabled_count == 0
        assert backup.metadata.disabled_count == 0
        assert len(backup.filters) == 0
