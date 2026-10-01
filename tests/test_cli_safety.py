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
    read_error = None  # set to a SieveReadError to make the live read fail
    covering = None  # scraped dicts the live section is built from (default: raw_filters)

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
        if FakeScraper.read_error:
            raise FakeScraper.read_error
        from src.parser.filter_parser import parse_scraped_filters
        from src.consolidator.consolidation_engine import ConsolidationEngine
        from src.generator.sieve_generator import SieveGenerator
        covering = FakeScraper.covering if FakeScraper.covering is not None else FakeScraper.raw_filters
        filters = parse_scraped_filters(covering)
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
    FakeScraper.read_error = None
    FakeScraper.covering = None
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

    def test_failed_sieve_read_saves_nothing(self, cli_snapshots_dir, fake_scraper):
        from src.scraper.browser import SieveReadError
        fake_scraper.raw_filters = [_raw_filter("Good")]
        fake_scraper.read_error = SieveReadError("the Sieve editor did not open")
        result = runner.invoke(app, ["backup", "--headless", "--allow-incomplete"])
        assert result.exit_code == 1
        assert "Could not read the live Sieve script" in result.output
        assert "the Sieve editor did not open" in result.output
        assert not (cli_snapshots_dir / "latest").exists()

    def test_allow_incomplete_saves_flagged(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [_raw_filter("Odd", issues=["label row unreadable"])]
        result = runner.invoke(app, ["backup", "--headless", "--allow-incomplete"])
        assert result.exit_code == 0, result.output
        data = json.loads((cli_snapshots_dir / "latest" / "backup.json").read_text())
        assert data["filters"][0]["scrape_issues"] == ["label row unreadable"]


def _unparseable_filter(name="Broken", enabled=True):
    """A scraped dict parse_filter raises on (a condition entry that is not a dict)."""
    raw = _raw_filter(name, enabled=enabled)
    raw["conditions"] = [None]
    return raw


class TestUnparseableFilter:
    """A filter that cannot be parsed is kept as a flagged stub, never dropped (P16)."""

    def test_unparseable_filter_refuses(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [_raw_filter("Good"), _unparseable_filter("Broken")]
        result = runner.invoke(app, ["backup", "--headless"])
        assert result.exit_code == 1
        assert "- Broken" in result.output
        assert "could not be parsed" in result.output
        assert "Backup NOT saved" in result.output
        assert not (cli_snapshots_dir / "latest").exists()

    def test_allow_incomplete_keeps_flagged_stub(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [_raw_filter("Good"), _unparseable_filter("Broken")]
        result = runner.invoke(app, ["backup", "--headless", "--allow-incomplete"])
        assert result.exit_code == 0, result.output
        data = json.loads((cli_snapshots_dir / "latest" / "backup.json").read_text())
        assert [f["name"] for f in data["filters"]] == ["Good", "Broken"]
        stub = data["filters"][1]
        assert any("could not be parsed" in i for i in stub["scrape_issues"])
        assert stub["raw"]["conditions_text"] == "the sender"


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

    def test_unparseable_stub_never_counts_as_covered(self, cli_snapshots_dir, fake_scraper, fake_sync):
        """A stub has no rules, so "all its rules are live" is vacuously true; it must
        still be held back, even with --allow-incomplete."""
        _backup(cli_snapshots_dir, [_unparseable_filter("Broken", enabled=False)])
        fake_scraper.raw_filters = [_unparseable_filter("Broken", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless", "--allow-incomplete"], input="y\n")
        assert result.exit_code == 1, result.output
        assert "not fully read" in result.output
        assert fake_sync.deleted == []


def _snapshot_files(snapshots_dir):
    """Every file under the snapshots dir with its bytes, to prove nothing was written."""
    return {p: p.read_bytes() for p in sorted(snapshots_dir.rglob("*")) if p.is_file()}


def _two_condition_filter(name, enabled=False, label="X"):
    """"sender is a OR sender is b -> label X"."""
    raw = _raw_filter(name, enabled=enabled, actions=[{"type": "label", "parameters": {"label": label}}])
    raw["logic"] = "or"
    raw["conditions"] = [
        {"type": "sender", "operator": "is", "value": "a@example.com"},
        {"type": "sender", "operator": "is", "value": "b@example.com"},
    ]
    return raw


def _only_a(label="X"):
    """A filter whose rule is the a@example.com half of _two_condition_filter."""
    raw = _raw_filter("Only A", actions=[{"type": "label", "parameters": {"label": label}}])
    raw["conditions"] = [{"type": "sender", "operator": "is", "value": "a@example.com"}]
    return raw


class TestCleanupCoverage:
    """Mutation-run gaps: coverage needs EVERY rule of EVERY filter in the live section."""

    def test_partially_covered_filter_kept(self, cli_snapshots_dir, fake_scraper, fake_sync):
        """Live section has only the a@ half of "a or b -> label X": not covered."""
        both = _two_condition_filter("A or B")
        _backup(cli_snapshots_dir, [both])
        fake_scraper.raw_filters = [both]
        fake_scraper.covering = [_only_a()]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 1, result.output
        assert "- A or B" in result.output
        assert fake_sync.deleted == []

    def test_cleanup_holds_back_every_uncovered_filter(self, cli_snapshots_dir, fake_scraper, fake_sync):
        covered = _raw_filter("Covered", enabled=False)
        gone1 = _raw_filter("Gone One", enabled=False)
        gone2 = _raw_filter("Gone Two", enabled=False)
        _backup(cli_snapshots_dir, [covered, gone1, gone2])
        fake_scraper.raw_filters = [covered, gone1, gone2]
        fake_scraper.covering = [covered]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 1, result.output
        assert fake_sync.deleted == ["Covered"]


class TestCleanupWritesOnlyAfterConfirmation:
    """P10: the archive is written only for filters actually being deleted, after 'y'."""

    def test_dry_run_writes_nothing(self, cli_snapshots_dir, fake_scraper, fake_sync):
        _backup(cli_snapshots_dir, [_raw_filter("Old", enabled=False)])
        fake_scraper.raw_filters = [_raw_filter("Old", enabled=False)]
        before = _snapshot_files(cli_snapshots_dir)
        result = runner.invoke(app, ["cleanup", "--headless", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "1 would be" in result.output
        assert _snapshot_files(cli_snapshots_dir) == before
        assert fake_sync.deleted == []

    def test_declined_confirmation_writes_nothing(self, cli_snapshots_dir, fake_scraper, fake_sync):
        _backup(cli_snapshots_dir, [_raw_filter("Old", enabled=False)])
        fake_scraper.raw_filters = [_raw_filter("Old", enabled=False)]
        before = _snapshot_files(cli_snapshots_dir)
        result = runner.invoke(app, ["cleanup", "--headless"], input="n\n")
        assert "Cleanup cancelled" in result.output
        assert _snapshot_files(cli_snapshots_dir) == before
        assert fake_sync.deleted == []

    def test_deleted_covered_filter_archived_as_archived(self, cli_snapshots_dir, fake_scraper, fake_sync):
        from src.models.filter_models import FilterStatus
        _backup(cli_snapshots_dir, [_raw_filter("Old", enabled=False)])
        fake_scraper.raw_filters = [_raw_filter("Old", enabled=False)]
        result = runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        assert result.exit_code == 0, result.output
        archive = BackupManager(cli_snapshots_dir).load_archive(cli_snapshots_dir / "latest")
        assert [(e.filter.name, e.filter.status) for e in archive] == [("Old", FilterStatus.ARCHIVED)]

    def test_held_back_filters_not_archived(self, cli_snapshots_dir, fake_scraper, fake_sync):
        """Only what is deleted is archived: not the uncovered filter cleanup kept."""
        covered = _raw_filter("Covered", enabled=False)
        gone = _raw_filter("Gone", enabled=False)
        _backup(cli_snapshots_dir, [covered, gone])
        fake_scraper.raw_filters = [covered, gone]
        fake_scraper.covering = [covered]
        runner.invoke(app, ["cleanup", "--headless"], input="y\n")
        archive = BackupManager(cli_snapshots_dir).load_archive(cli_snapshots_dir / "latest")
        assert [e.filter.name for e in archive] == ["Covered"]

    def test_include_uncovered_archives_as_deprecated(self, cli_snapshots_dir, fake_scraper, fake_sync):
        """A deliberately disabled filter whose rules are not live is deleted, but its
        archive copy must not put the rule into the next script (the panel's repro)."""
        from src.models.filter_models import FilterStatus
        gone = _raw_filter("Gone", enabled=False, actions=[{"type": "delete", "parameters": {}}])
        _backup(cli_snapshots_dir, [gone])
        fake_scraper.raw_filters = [gone]
        fake_scraper.covering = [_raw_filter("Something Else")]
        result = runner.invoke(app, ["cleanup", "--headless", "--include-uncovered"], input="y\n")
        assert result.exit_code == 0, result.output
        assert fake_sync.deleted == ["Gone"]
        archive = BackupManager(cli_snapshots_dir).load_archive(cli_snapshots_dir / "latest")
        assert [(e.filter.name, e.filter.status) for e in archive] == [("Gone", FilterStatus.DEPRECATED)]

        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        script = (cli_snapshots_dir / "latest" / "consolidated.sieve").read_text()
        assert "Gone@example.com" not in script
        assert "discard" not in script

