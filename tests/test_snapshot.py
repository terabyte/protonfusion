"""Tests for snapshot CLI commands (view, set-status, remove)."""

import json
import os
import pytest
from pathlib import Path

from typer.testing import CliRunner

import src.utils.config
from src.main import app, console
from src.backup.backup_manager import BackupManager
from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, FilterStatus,
    ConditionType, Operator, ActionType,
)
from src.models.backup_models import ArchiveEntry


runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """Ensure Rich console is wide enough for full table output in CliRunner."""
    import src.main
    from rich.console import Console
    wide_console = Console(width=200)
    monkeypatch.setattr(src.main, "console", wide_console)


@pytest.fixture
def cli_snapshots_dir(tmp_path, monkeypatch):
    """Set up a temp snapshots dir that the CLI will use via BackupManager()."""
    import src.backup.backup_manager
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", snapshots_dir)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", snapshots_dir)
    return snapshots_dir


class TestSnapshotView:
    """Test 'snapshot view' command."""

    def test_view_backup_only(self, cli_snapshots_dir, sample_filters_list):
        """Test viewing snapshot with only backup filters."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)

        result = runner.invoke(app, ["snapshot", "view"])
        assert result.exit_code == 0
        assert "Spam Filter 1" in result.output
        assert "Move to Spam" in result.output

    def test_view_backup_plus_archive(self, cli_snapshots_dir, sample_filters_list):
        """Test viewing snapshot with backup + archive filters."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        archived_filter = ProtonMailFilter(
            name="Archived Rule",
            status=FilterStatus.ARCHIVED,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="old@test.com")],
            actions=[FilterAction(type=ActionType.DELETE)],
        )
        manager.write_archive(snapshot_dir, [ArchiveEntry(filter=archived_filter)])

        result = runner.invoke(app, ["snapshot", "view"])
        assert result.exit_code == 0
        assert "Archived Rule" in result.output
        assert "archived" in result.output.lower()

    def test_view_shows_status_summary(self, cli_snapshots_dir, sample_filters_list):
        """Test that view shows status summary panel."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)

        result = runner.invoke(app, ["snapshot", "view"])
        assert result.exit_code == 0
        assert "Snapshot View" in result.output

    def test_view_empty_snapshot(self, cli_snapshots_dir):
        """Test viewing snapshot with no filters."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([])

        result = runner.invoke(app, ["snapshot", "view"])
        assert result.exit_code == 0
        assert "No filters" in result.output

    def test_view_archive_overrides_backup(self, cli_snapshots_dir, sample_filters_list):
        """Test that archive entries override backup entries with same content_hash."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        # Create archive entry for the same filter as in backup but with archived status
        f = sample_filters_list[0].model_copy(deep=True)
        f.status = FilterStatus.ARCHIVED
        manager.write_archive(snapshot_dir, [ArchiveEntry(filter=f)])

        result = runner.invoke(app, ["snapshot", "view"])
        assert result.exit_code == 0
        # The filter should appear only once (archive overrides backup)
        assert result.output.count("Spam Filter 1") == 1


class TestSnapshotSetStatus:
    """Test 'snapshot set-status' command."""

    def test_set_status_from_backup(self, cli_snapshots_dir, sample_filters_list):
        """Test setting status of a backup filter creates archive entry."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)

        result = runner.invoke(app, ["snapshot", "set-status", "Spam Filter 1", "deprecated"])
        assert result.exit_code == 0
        assert "deprecated" in result.output

        # Verify archive was created
        snapshot_dir = manager.snapshot_dir_for("latest")
        entries = manager.load_archive(snapshot_dir)
        assert len(entries) == 1
        assert entries[0].filter.name == "Spam Filter 1"
        assert entries[0].filter.status == FilterStatus.DEPRECATED

    def test_set_status_updates_existing_archive(self, cli_snapshots_dir, sample_filters_list):
        """Test updating status of an existing archive entry."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        # Create initial archive entry
        f = sample_filters_list[0].model_copy(deep=True)
        f.status = FilterStatus.ARCHIVED
        manager.write_archive(snapshot_dir, [ArchiveEntry(filter=f)])

        # Update status
        result = runner.invoke(app, ["snapshot", "set-status", "Spam Filter 1", "deprecated"])
        assert result.exit_code == 0
        assert "Updated" in result.output

        # Verify updated
        entries = manager.load_archive(snapshot_dir)
        assert len(entries) == 1
        assert entries[0].filter.status == FilterStatus.DEPRECATED

    def test_set_status_filter_not_found(self, cli_snapshots_dir, sample_filters_list):
        """Test setting status of non-existent filter."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)

        result = runner.invoke(app, ["snapshot", "set-status", "NonExistent", "archived"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_set_status_valid_transitions(self, cli_snapshots_dir, sample_filters_list):
        """Test all valid status values."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)

        for status in ["enabled", "disabled", "archived", "deprecated"]:
            result = runner.invoke(app, ["snapshot", "set-status", "Spam Filter 1", status])
            assert result.exit_code == 0, f"Failed for status={status}: {result.output}"


class TestSnapshotRemove:
    """Test 'snapshot remove' command."""

    def test_remove_from_archive(self, cli_snapshots_dir, sample_filters_list):
        """Test removing a filter from archive."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)
        snapshot_dir = manager.snapshot_dir_for("latest")

        f = sample_filters_list[0].model_copy(deep=True)
        f.status = FilterStatus.ARCHIVED
        manager.write_archive(snapshot_dir, [ArchiveEntry(filter=f)])

        result = runner.invoke(app, ["snapshot", "remove", "Spam Filter 1"])
        assert result.exit_code == 0
        assert "Removed" in result.output

        # Verify removed
        entries = manager.load_archive(snapshot_dir)
        assert len(entries) == 0

    def test_remove_backup_only_filter_errors(self, cli_snapshots_dir, sample_filters_list):
        """Test that removing a filter only in backup (not archive) errors."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)

        result = runner.invoke(app, ["snapshot", "remove", "Spam Filter 1"])
        assert result.exit_code == 1
        assert "immutable" in result.output.lower() or "backup" in result.output.lower()

    def test_remove_nonexistent_filter(self, cli_snapshots_dir, sample_filters_list):
        """Test removing a filter that doesn't exist anywhere."""
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup(sample_filters_list)

        result = runner.invoke(app, ["snapshot", "remove", "Ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()
