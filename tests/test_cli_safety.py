"""CLI tests for the data-loss guards on `backup` and `cleanup`.

The browser classes are replaced with fakes, so these run offline: they
check what the commands do with scraped data, not the scraping itself.
"""

import json

import pytest
from typer.testing import CliRunner

import src.utils.config
import src.scraper.protonmail_scraper
from src.main import app
from src.backup.backup_manager import BackupManager

runner = CliRunner()


def _raw_filter(name, enabled=True, issues=None, raw=True, actions=None):
    """A scraped-filter dict as ProtonMailScraper returns it."""
    return {
        "name": name,
        "enabled": enabled,
        "priority": 0,
        "logic": "and",
        "conditions": [{"type": "sender", "operator": "contains", "value": f"{name}@example.com"}],
        "actions": actions if actions is not None else [{"type": "label", "parameters": {"label": "Work"}}],
        "raw": {"conditions_text": "the sender", "actions_text": "Label as Work", "sieve_text": ""} if raw else None,
        "scrape_issues": issues or [],
    }


class FakeScraper:
    """Stands in for ProtonMailScraper; returns FakeScraper.raw_filters."""
    raw_filters: list = []

    def __init__(self, *args, **kwargs):
        self.account_email = "test@proton.me"

    async def initialize(self):
        pass

    async def login(self):
        pass

    async def navigate_to_filters(self):
        pass

    async def scrape_all_filters(self, workers=1):
        return FakeScraper.raw_filters

    async def read_sieve_script(self, filter_name=""):
        """A live script whose ProtonFusion section covers every scraped filter.

        cleanup also refuses filters whose rules are not in the live section
        (the sync-safety guard); these tests exercise the backup-completeness
        guard, so the coverage guard is satisfied here.
        """
        from src.parser.filter_parser import parse_scraped_filters
        from src.consolidator.consolidation_engine import ConsolidationEngine
        from src.generator.sieve_generator import SieveGenerator
        filters = parse_scraped_filters(FakeScraper.raw_filters)
        consolidated, _ = ConsolidationEngine().consolidate(filters, include_disabled=True)
        return SieveGenerator.merge_with_existing(SieveGenerator().generate(consolidated), "")

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """Keep Rich from wrapping long names/issues in CliRunner output."""
    import src.main
    from rich.console import Console
    monkeypatch.setattr(src.main, "console", Console(width=200))


@pytest.fixture
def cli_snapshots_dir(tmp_path, monkeypatch):
    """Temp snapshots dir used by the CLI's BackupManager()."""
    import src.backup.backup_manager
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", snapshots_dir)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", snapshots_dir)
    return snapshots_dir


@pytest.fixture
def fake_scraper(monkeypatch):
    monkeypatch.setattr(src.scraper.protonmail_scraper, "ProtonMailScraper", FakeScraper)
    FakeScraper.raw_filters = []
    return FakeScraper


class TestBackupGuard:
    """`backup` must fail loudly when a filter was not fully read."""

    def test_complete_scrape_saves(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [_raw_filter("Good")]
        result = runner.invoke(app, ["backup", "--headless"])
        assert result.exit_code == 0, result.output
        data = json.loads((cli_snapshots_dir / "latest" / "backup.json").read_text())
        assert data["filters"][0]["actions"][0]["parameters"]["label"] == "Work"
        assert data["filters"][0]["raw"]["actions_text"] == "Label as Work"

    def test_incomplete_scrape_fails_and_names_filter(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [
            _raw_filter("Good"),
            _raw_filter("Has Autoreply", issues=["unsupported action row 'filter-modal:autoreply-row'"]),
        ]
        result = runner.invoke(app, ["backup", "--headless"])
        assert result.exit_code == 1
        assert "Has Autoreply" in result.output
        assert "filter-modal:autoreply-row" in result.output
        assert "Backup NOT saved" in result.output
        assert not (cli_snapshots_dir / "latest").exists()

    def test_allow_incomplete_saves_flagged(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [_raw_filter("Odd", issues=["label row unreadable"])]
        result = runner.invoke(app, ["backup", "--headless", "--allow-incomplete"])
        assert result.exit_code == 0, result.output
        data = json.loads((cli_snapshots_dir / "latest" / "backup.json").read_text())
        assert data["filters"][0]["scrape_issues"] == ["label row unreadable"]


class FakeSync:
    """Stands in for ProtonMailSync; records which filters were deleted."""
    deleted: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def initialize(self):
        pass

    async def login(self):
        pass

    async def navigate_to_filters(self):
        pass

    async def delete_filter(self, name):
        FakeSync.deleted.append(name)
        return True

    async def close(self):
        pass


@pytest.fixture
def fake_sync(monkeypatch):
    import src.scraper.protonmail_sync
    monkeypatch.setattr(src.scraper.protonmail_sync, "ProtonMailSync", FakeSync)
    FakeSync.deleted = []
    return FakeSync


def _backup(snapshots_dir, raw_filters):
    """Write a snapshot holding the given scraped filters."""
    from src.parser.filter_parser import parse_scraped_filters
    BackupManager(snapshots_dir).create_backup(parse_scraped_filters(raw_filters))


class TestCleanupGuard:
    """`cleanup` must not delete a filter without a complete backup copy."""

    def test_deletes_verified_filter(self, cli_snapshots_dir, fake_scraper, fake_sync):
        _backup(cli_snapshots_dir, [_raw_filter("Old", enabled=False)])
        fake_scraper.raw_filters = [_raw_filter("Old", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 0, result.output
        assert fake_sync.deleted == ["Old"]

    def test_refuses_incomplete_backup_copy(self, cli_snapshots_dir, fake_scraper, fake_sync):
        _backup(cli_snapshots_dir, [
            _raw_filter("Safe", enabled=False),
            _raw_filter("Partial", enabled=False, issues=["label row unreadable"]),
        ])
        fake_scraper.raw_filters = [
            _raw_filter("Safe", enabled=False),
            _raw_filter("Partial", enabled=False),
        ]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 1
        assert "Refusing to delete 1" in result.output
        assert "Partial" in result.output
        assert "label row unreadable" in result.output
        assert fake_sync.deleted == ["Safe"]

    def test_refuses_backup_without_raw_evidence(self, cli_snapshots_dir, fake_scraper, fake_sync):
        """A pre-1.1 backup holds no evidence; it is exactly the kind that lost labels."""
        _backup(cli_snapshots_dir, [_raw_filter("Legacy", enabled=False, raw=False)])
        fake_scraper.raw_filters = [_raw_filter("Legacy", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 1
        assert "no raw evidence" in result.output
        assert fake_sync.deleted == []

    def test_refuses_when_backup_differs_from_live(self, cli_snapshots_dir, fake_scraper, fake_sync):
        """An old backup that recorded no label does not cover a live filter that has one."""
        _backup(cli_snapshots_dir, [_raw_filter("Labelled", enabled=False, actions=[])])
        fake_scraper.raw_filters = [_raw_filter("Labelled", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 1
        assert "differs from the live filter" in result.output
        assert fake_sync.deleted == []

    def test_refuses_without_any_snapshot(self, cli_snapshots_dir, fake_scraper, fake_sync):
        fake_scraper.raw_filters = [_raw_filter("Orphan", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 1
        assert "no backup copy" in result.output
        assert fake_sync.deleted == []

    def test_allow_incomplete_overrides(self, cli_snapshots_dir, fake_scraper, fake_sync):
        _backup(cli_snapshots_dir, [_raw_filter("Partial", enabled=False, issues=["x"])])
        fake_scraper.raw_filters = [_raw_filter("Partial", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless", "--allow-incomplete"], input="y\n")
        assert result.exit_code == 0, result.output
        assert fake_sync.deleted == ["Partial"]

    @pytest.mark.parametrize("backed_up, reason", [
        (_raw_filter("Labelled", enabled=False, actions=[]), "differs from the live filter"),
        (_raw_filter("Labelled", enabled=False, raw=False), "no raw evidence"),
    ])
    def test_second_run_still_refuses(self, cli_snapshots_dir, fake_scraper, fake_sync, backed_up, reason):
        """The auto-archive must not turn a refused live filter into its own backup copy."""
        _backup(cli_snapshots_dir, [backed_up])
        fake_scraper.raw_filters = [_raw_filter("Labelled", enabled=False)]
        for _ in range(2):
            result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
            assert result.exit_code == 1, result.output
            assert reason in result.output
        assert fake_sync.deleted == []

    def test_allow_incomplete_still_archives(self, cli_snapshots_dir, fake_scraper, fake_sync):
        """With the override the filter is deleted, so the archive keeps the live copy."""
        _backup(cli_snapshots_dir, [_raw_filter("Partial", enabled=False, issues=["x"])])
        fake_scraper.raw_filters = [_raw_filter("Partial", enabled=False)]
        runner.invoke(app, ["cleanup", "--headless", "--allow-incomplete"], input="y\n")
        archive = BackupManager(cli_snapshots_dir).load_archive(cli_snapshots_dir / "latest")
        assert [e.filter.name for e in archive] == ["Partial"]

    def test_dry_run_reports_refusal(self, cli_snapshots_dir, fake_scraper, fake_sync):
        fake_scraper.raw_filters = [_raw_filter("Orphan", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless", "--dry-run"])
        assert result.exit_code == 1
        assert "0 would be" in result.output
        assert fake_sync.deleted == []
