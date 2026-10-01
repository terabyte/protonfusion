"""Tests for the sync/cleanup safety guards (no browser: ProtonMailSync is faked)."""

import pytest
from typer.testing import CliRunner

import src.utils.config
from src.main import app, SIEVE_FILTER_NAME
from src.backup.backup_manager import BackupManager
from src.generator.sieve_generator import SieveGenerator
from src.models.filter_models import (
    ScrapeEvidence,
    ProtonMailFilter, FilterCondition, FilterAction,
    ConditionType, Operator, ActionType,
)


runner = CliRunner()


def _filter(sender: str, folder: str = "Spam", enabled: bool = True) -> ProtonMailFilter:
    return ProtonMailFilter(
        name=f"Filter {sender}",
        enabled=enabled,
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value=sender)],
        actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": folder})],
        # A complete scrape, so cleanup's backup-completeness guard passes and
        # these tests exercise only the live-section coverage guard.
        raw=ScrapeEvidence(conditions_text="the sender", actions_text=f"Move to {folder}"),
    )


def _section_for(filters) -> str:
    """Build a full live script whose ProtonFusion section holds `filters`."""
    from src.consolidator.consolidation_engine import ConsolidationEngine
    consolidated, _ = ConsolidationEngine().consolidate(filters, include_disabled=True)
    return SieveGenerator.merge_with_existing(SieveGenerator().generate(consolidated), "")


class FakeSync:
    """Stand-in for ProtonMailSync that records every write operation."""

    live_script = ""
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def initialize(self):
        pass

    async def login(self):
        return True

    async def navigate_to_filters(self):
        pass

    async def read_sieve_script(self, filter_name=""):
        return type(self).live_script

    async def disable_all_ui_filters(self):
        type(self).calls.append("disable_all")
        return 3

    async def upload_sieve(self, script, filter_name=""):
        type(self).calls.append(("upload", script))
        return True

    async def delete_filter(self, name):
        type(self).calls.append(("delete", name))
        return True

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    import src.main
    from rich.console import Console
    monkeypatch.setattr(src.main, "console", Console(width=200))


@pytest.fixture
def cli_snapshots_dir(tmp_path, monkeypatch):
    import src.backup.backup_manager
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", snapshots_dir)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", snapshots_dir)
    return snapshots_dir


@pytest.fixture
def fake_sync(monkeypatch):
    import src.scraper.protonmail_sync
    FakeSync.live_script = ""
    FakeSync.calls = []
    monkeypatch.setattr(src.scraper.protonmail_sync, "ProtonMailSync", FakeSync)
    return FakeSync


@pytest.fixture
def shrunk_account(cli_snapshots_dir, fake_sync):
    """The real-world state: 20 rules live in Sieve, only 2 UI filters left.

    Returns the live script. The latest snapshot holds a backup of the 2
    surviving filters (with the live script captured) and a consolidated.sieve
    generated from just those 2.
    """
    all_filters = [_filter(f"s{i}@x.com", folder=f"F{i % 3}") for i in range(20)]
    live = _section_for(all_filters)
    fake_sync.live_script = live

    manager = BackupManager(cli_snapshots_dir)
    manager.create_backup(all_filters[:2], sieve_script=live)
    result = runner.invoke(app, ["consolidate"])
    assert result.exit_code == 0, result.output
    return live


class TestSyncRefusesToDropRules:

    def test_sync_refuses_and_touches_nothing(self, shrunk_account, fake_sync):
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "drop 18" in result.output
        assert "s19@x.com" in result.output
        assert "Sync refused" in result.output
        assert fake_sync.calls == []

    def test_sync_with_override_proceeds(self, shrunk_account, fake_sync):
        result = runner.invoke(app, ["sync", "--allow-rule-removal"])
        assert result.exit_code == 0, result.output
        assert "disable_all" in fake_sync.calls
        assert any(c[0] == "upload" for c in fake_sync.calls if isinstance(c, tuple))

    def test_dry_run_reports_refusal(self, shrunk_account, fake_sync):
        result = runner.invoke(app, ["sync", "--dry-run"])
        assert result.exit_code == 1
        assert "would REFUSE" in result.output
        assert "s19@x.com" in result.output
        assert fake_sync.calls == []

    def test_show_diff_only_reports_refusal(self, shrunk_account, fake_sync):
        result = runner.invoke(app, ["sync", "--show-diff-only"])
        assert result.exit_code == 1
        assert "would REFUSE" in result.output
        assert fake_sync.calls == []

    def test_empty_live_read_with_backed_up_section_refuses(self, shrunk_account, fake_sync):
        fake_sync.live_script = ""
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "read back empty" in result.output
        assert fake_sync.calls == []

    def test_unparseable_live_section_refuses(self, shrunk_account, fake_sync):
        fake_sync.live_script = shrunk_account.replace("}", "", 1)
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "Could not parse" in result.output
        assert fake_sync.calls == []


class TestSyncAllowsSafeChanges:

    def test_superset_sync_proceeds(self, cli_snapshots_dir, fake_sync):
        filters = [_filter("a@x.com"), _filter("b@x.com")]
        fake_sync.live_script = _section_for(filters[:1])
        BackupManager(cli_snapshots_dir).create_backup(filters, sieve_script=fake_sync.live_script)
        assert runner.invoke(app, ["consolidate"]).exit_code == 0

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "No rules dropped" in result.output
        assert "disable_all" in fake_sync.calls

    def test_first_sync_without_live_section_proceeds(self, cli_snapshots_dir, fake_sync):
        BackupManager(cli_snapshots_dir).create_backup([_filter("a@x.com")])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "disable_all" in fake_sync.calls


def test_dropped_listing_escapes_rich_markup(cli_snapshots_dir, fake_sync):
    """Subjects like "[SPAM]" must be printed literally, not eaten as markup."""
    spam = ProtonMailFilter(
        name="tagged",
        conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="[SPAM]")],
        actions=[FilterAction(type=ActionType.DELETE)],
    )
    fake_sync.live_script = _section_for([spam, _filter("a@x.com")])
    BackupManager(cli_snapshots_dir).create_backup([_filter("a@x.com")], sieve_script=fake_sync.live_script)
    assert runner.invoke(app, ["consolidate"]).exit_code == 0

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert '"[spam]"' in result.output


class FakeScraper(FakeSync):
    """Stand-in for ProtonMailScraper; returns already-parsed filters."""

    filters: list = []

    async def scrape_all_filters(self, workers=1):
        return list(type(self).filters)


@pytest.fixture
def fake_scraper(monkeypatch, fake_sync):
    import src.main
    import src.scraper.protonmail_scraper
    FakeScraper.filters = []
    monkeypatch.setattr(src.scraper.protonmail_scraper, "ProtonMailScraper", FakeScraper)
    monkeypatch.setattr(src.main, "parse_scraped_filters", lambda raw: raw)
    return FakeScraper


class TestCleanupOnlyDeletesCoveredFilters:

    def test_uncovered_disabled_filter_is_kept(self, cli_snapshots_dir, fake_sync, fake_scraper):
        covered = _filter("in-sieve@x.com", enabled=False)
        orphan = _filter("nowhere@x.com", enabled=False)
        fake_scraper.filters = [covered, orphan]
        fake_sync.live_script = _section_for([covered])
        BackupManager(cli_snapshots_dir).create_backup([covered, orphan])

        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 0, result.output
        assert ("delete", covered.name) in fake_sync.calls
        assert ("delete", orphan.name) not in fake_sync.calls
        assert "NOT in the live" in result.output

    def test_no_live_section_deletes_nothing(self, cli_snapshots_dir, fake_sync, fake_scraper):
        fake_scraper.filters = [_filter("a@x.com", enabled=False)]
        fake_sync.live_script = ""
        BackupManager(cli_snapshots_dir).create_backup(fake_scraper.filters)

        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 0, result.output
        assert fake_sync.calls == []
        assert "Nothing safe to delete" in result.output

    def test_include_uncovered_overrides(self, cli_snapshots_dir, fake_sync, fake_scraper):
        orphan = _filter("nowhere@x.com", enabled=False)
        fake_scraper.filters = [orphan]
        fake_sync.live_script = ""
        BackupManager(cli_snapshots_dir).create_backup([orphan])

        result = runner.invoke(app, ["cleanup", "--include-uncovered"], input="y\n")
        assert result.exit_code == 0, result.output
        assert ("delete", orphan.name) in fake_sync.calls

    def test_after_refused_sync_cleanup_deletes_nothing(self, shrunk_account, fake_sync, fake_scraper):
        """The sync refusal leaves the old section live; filters only it lacks survive cleanup."""
        assert runner.invoke(app, ["sync"]).exit_code == 1
        new_filter = _filter("brand-new@x.com", enabled=False)
        fake_scraper.filters = [new_filter]
        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 0
        assert fake_sync.calls == []


class TestSieveFiltersLeftAlone:
    """A Sieve filter is a script, so it is never consolidated or deleted.

    The failure this guards: a sync disables every filter, including
    SIEVE_FILTER_NAME, then fails to upload. The scraped Sieve filter has no
    conditions or actions, so it used to read as an unconditional `keep;`.
    Once any earlier consolidation had folded a Sieve filter in the same way,
    the live section held that `keep;` too, so the coverage guard called the
    filter covered and cleanup deleted it.
    """

    @staticmethod
    def _protonfusion_filter(script: str, enabled: bool = False) -> ProtonMailFilter:
        # Built the way a backup written before is_sieve existed reads back:
        # only the captured script marks it as a Sieve filter.
        return ProtonMailFilter(name=SIEVE_FILTER_NAME, enabled=enabled, raw=ScrapeEvidence(sieve_text=script))

    def test_cleanup_keeps_disabled_protonfusion_filter(self, cli_snapshots_dir, fake_sync, fake_scraper):
        covered = _filter("in-sieve@x.com", enabled=False)
        # The `keep;` an earlier run emitted for a Sieve filter it consolidated
        earlier_sieve = ProtonMailFilter(name="Earlier Sieve filter")
        fake_sync.live_script = _section_for([covered, earlier_sieve])
        pf = self._protonfusion_filter(fake_sync.live_script)
        fake_scraper.filters = [covered, pf]
        BackupManager(cli_snapshots_dir).create_backup([covered, pf], sieve_script=fake_sync.live_script)

        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 0, result.output
        assert ("delete", covered.name) in fake_sync.calls
        assert ("delete", SIEVE_FILTER_NAME) not in fake_sync.calls
        assert "Leaving disabled Sieve filters alone" in result.output

    def test_include_uncovered_still_keeps_sieve_filters(self, cli_snapshots_dir, fake_sync, fake_scraper):
        # An empty script: only the explicit flag marks it
        pf = ProtonMailFilter(
            name=SIEVE_FILTER_NAME, enabled=False, is_sieve=True, raw=ScrapeEvidence(sieve_text=""),
        )
        fake_scraper.filters = [pf]
        BackupManager(cli_snapshots_dir).create_backup([pf])

        result = runner.invoke(app, ["cleanup", "--include-uncovered", "--allow-incomplete"], input="y\n")
        assert result.exit_code == 0, result.output
        assert fake_sync.calls == []

    def test_consolidate_skips_sieve_filter(self, cli_snapshots_dir, fake_sync):
        wizard = _filter("a@x.com")
        live = _section_for([wizard])
        pf = self._protonfusion_filter(live, enabled=True)
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([wizard, pf], sieve_script=live)

        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Sieve filters (left as they are): 1" in result.output
        script = (manager.snapshot_dir_for("latest") / "consolidated.sieve").read_text()
        assert SIEVE_FILTER_NAME not in script
        archived = [e.filter.name for e in manager.load_archive(manager.snapshot_dir_for("latest"))]
        assert SIEVE_FILTER_NAME not in archived


class TestCleanupHoldsBackSharedNames:
    """delete_filter() works by name, so a name shared by any two scraped filters is never deleted."""

    @staticmethod
    def _named(f: ProtonMailFilter, name: str) -> ProtonMailFilter:
        return f.model_copy(update={"name": name})

    def test_covered_and_uncovered_with_same_name(self, cli_snapshots_dir, fake_sync, fake_scraper):
        covered = self._named(_filter("in-sieve@x.com", enabled=False), "News")
        uncovered = self._named(_filter("nowhere@x.com", enabled=False), "News")
        other = _filter("other@x.com", enabled=False)
        fake_scraper.filters = [covered, uncovered, other]
        fake_sync.live_script = _section_for([covered, other])
        BackupManager(cli_snapshots_dir).create_backup([covered, uncovered, other])

        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert ("delete", "News") not in fake_sync.calls
        assert ("delete", other.name) in fake_sync.calls
        assert "name is shared" in result.output

    def test_disabled_and_enabled_with_same_name(self, cli_snapshots_dir, fake_sync, fake_scraper):
        disabled = self._named(_filter("old@x.com", enabled=False), "News")
        enabled = self._named(_filter("live@x.com", enabled=True), "News")
        fake_scraper.filters = [disabled, enabled]
        fake_sync.live_script = _section_for([disabled])
        BackupManager(cli_snapshots_dir).create_backup([disabled, enabled])

        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert fake_sync.calls == []
        assert "name is shared" in result.output

    def test_include_uncovered_does_not_override(self, cli_snapshots_dir, fake_sync, fake_scraper):
        a = self._named(_filter("a@x.com", enabled=False), "News")
        b = self._named(_filter("b@x.com", enabled=False), "News")
        fake_scraper.filters = [a, b]
        BackupManager(cli_snapshots_dir).create_backup([a, b])

        runner.invoke(app, ["cleanup", "--include-uncovered", "--allow-incomplete"], input="y\n")
        assert fake_sync.calls == []


class TestFiltersWithoutEvidence:
    """Filters from a pre-1.1 backup (raw None) may be missing labels; sync refuses them."""

    @pytest.fixture
    def legacy_snapshot(self, cli_snapshots_dir, fake_sync):
        """A backup holding one filter as format 1.0 recorded it: no raw evidence."""
        legacy = _filter("old@x.com").model_copy(update={"raw": None})
        BackupManager(cli_snapshots_dir).create_backup([legacy, _filter("new@x.com")])
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        return result

    def test_consolidate_warns(self, legacy_snapshot):
        assert "no raw evidence" in legacy_snapshot.output
        assert "- Filter old@x.com" in legacy_snapshot.output
        assert "- Filter new@x.com" not in legacy_snapshot.output

    def test_sync_refuses(self, legacy_snapshot, fake_sync):
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "Filter old@x.com" in result.output
        assert "Sync refused" in result.output
        assert fake_sync.calls == []

    def test_dry_run_refuses(self, legacy_snapshot, fake_sync):
        result = runner.invoke(app, ["sync", "--dry-run"])
        assert result.exit_code == 1
        assert fake_sync.calls == []

    def test_allow_incomplete_proceeds(self, legacy_snapshot, fake_sync):
        result = runner.invoke(app, ["sync", "--allow-incomplete"])
        assert result.exit_code == 0, result.output
        assert "disable_all" in fake_sync.calls

    def test_explicit_other_script_not_checked(self, legacy_snapshot, fake_sync, tmp_path):
        """The manifest describes the snapshot's own script, not one given with --sieve."""
        other = tmp_path / "other.sieve"
        other.write_text(_section_for([_filter("new@x.com")]))
        result = runner.invoke(app, ["sync", "--sieve", str(other)])
        assert result.exit_code == 0, result.output
