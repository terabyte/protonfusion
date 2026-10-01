"""Cleanup guards on what counts as covered and what gets archived (no browser).

Each test pins one guard that a mutation run showed no other test held.
"""

from typer.testing import CliRunner

from src.backup.backup_manager import BackupManager
from src.main import app
from src.models.backup_models import ArchiveEntry
from src.models.filter_models import FilterStatus
from tests.test_sync_safety import (  # noqa: F401 (fixtures)
    _filter, _section_for, _wide_console, cli_snapshots_dir, fake_scraper, fake_sync,
)

runner = CliRunner()


def test_incomplete_filter_with_covered_partial_rules_is_kept_even_with_allow_incomplete(
    cli_snapshots_dir, fake_sync, fake_scraper,
):
    """The rules read so far are live, but the unread part may not be (CL2).

    --allow-incomplete is about the backup copy, not coverage: only
    --include-uncovered may delete a filter whose rules cannot be checked.
    """
    covered = _filter("a@x.com", enabled=False)
    partial = covered.model_copy(update={"scrape_issues": ["unsupported action row 'x'"]})
    fake_scraper.filters = [partial]
    fake_sync.live_script = _section_for([covered])
    BackupManager(cli_snapshots_dir).create_backup([partial])

    result = runner.invoke(app, ["cleanup", "--allow-incomplete"], input="y\n")
    assert result.exit_code == 1, result.output
    assert fake_sync.calls == []
    assert "not fully read, so its rules cannot be checked" in result.output


def test_hash_already_archived_is_not_archived_again(cli_snapshots_dir, fake_sync, fake_scraper):
    """A filter whose content is already in archive.json gets no second entry (CL4).

    Here it was archived as DEPRECATED earlier; a second ARCHIVED entry for
    the same hash would contradict it.
    """
    f = _filter("a@x.com", enabled=False)
    manager = BackupManager(cli_snapshots_dir)
    manager.create_backup([f])
    latest = manager.snapshot_dir_for("latest")
    earlier = f.model_copy(update={"status": FilterStatus.DEPRECATED})
    manager.write_archive(latest, [ArchiveEntry(filter=earlier, archived_at="t0", source_snapshot=latest.name)])
    fake_scraper.filters = [f]
    fake_sync.live_script = _section_for([f])

    result = runner.invoke(app, ["cleanup"], input="y\n")
    assert result.exit_code == 0, result.output
    assert ("delete", f.name) in fake_sync.calls
    entries = manager.load_archive(latest)
    assert [(e.filter.content_hash, e.filter.status) for e in entries] == [
        (f.content_hash, FilterStatus.DEPRECATED),
    ]


def test_cleanup_coverage_comes_only_from_live_section(cli_snapshots_dir, fake_sync, fake_scraper):
    """A filter's own rules never count as live coverage, raw evidence or not (C9)."""
    other = _filter("other@x.com", enabled=False)
    no_evidence = _filter("old@x.com", enabled=False).model_copy(update={"raw": None})
    fake_scraper.filters = [no_evidence]
    fake_sync.live_script = _section_for([other])
    BackupManager(cli_snapshots_dir).create_backup([no_evidence])

    result = runner.invoke(app, ["cleanup", "--allow-incomplete"], input="y\n")
    assert result.exit_code == 1, result.output
    assert fake_sync.calls == []
