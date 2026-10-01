"""Tests for the sync/cleanup safety guards (no browser: ProtonMailSync is faked)."""

import pytest
from typer.testing import CliRunner

import src.utils.config
from src.main import app, SIEVE_FILTER_NAME
from src.backup.backup_manager import BackupManager
from src.generator.sieve_generator import SieveGenerator
# The real class, imported before the fixtures below replace it with FakeSync
from src.scraper.protonmail_sync import ProtonMailSync as RealProtonMailSync
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
    read_error = None  # set to a SieveReadError to make the live read fail
    upload_result = True  # False, or an exception instance to raise
    hit_limit = False  # what upload_hit_filter_limit reports after a failed upload
    toggle_fails: set = set()  # (name, enabled) pairs set_row_enabled refuses
    toggle_raises: set = set()  # (name, enabled) pairs set_row_enabled raises on
    toggle_late: set = set()  # (name, enabled) pairs that change but are not seen to (W8)
    row_enabled: dict = {}  # name -> live toggle state, for require_current; default enabled
    upload_hit_filter_limit = False
    last_toggle_refused = False

    def __init__(self, *args, **kwargs):
        pass

    async def initialize(self):
        pass

    async def login(self):
        return True

    async def navigate_to_filters(self):
        pass

    async def read_sieve_script(self, filter_name=""):
        if type(self).read_error:
            raise type(self).read_error
        return type(self).live_script

    async def set_row_enabled(self, index, name, enabled, expected_names=None, require_current=None):
        self.last_toggle_refused = False
        if (name, enabled) in type(self).toggle_late:
            # The click took, but only after the read-back gave up
            type(self).calls.append(("enable" if enabled else "disable", name))
            self.last_toggle_refused = True
            return False
        if (name, enabled) in type(self).toggle_raises:
            raise RuntimeError(f"row for {name} detached")
        if (name, enabled) in type(self).toggle_fails:
            return False
        if require_current is not None and type(self).row_enabled.get(name, True) != require_current:
            return False
        type(self).calls.append(("enable" if enabled else "disable", name))
        return True

    async def upload_sieve(self, script, filter_name=""):
        type(self).calls.append(("upload", script))
        result = type(self).upload_result
        if isinstance(result, Exception):
            raise result
        self.upload_hit_filter_limit = not result and type(self).hit_limit
        return result

    async def delete_filter(self, name):
        type(self).calls.append(("delete", name))
        return True

    async def close(self):
        pass


class FakeScraper(FakeSync):
    """Stand-in for ProtonMailScraper; returns already-parsed filters."""

    filters: list = []

    async def scrape_all_filters(self, workers=1):
        return list(type(self).filters)


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
    """Fake both browser classes: sync scrapes with one and writes with the other."""
    import src.main
    import src.scraper.protonmail_scraper
    import src.scraper.protonmail_sync
    FakeSync.live_script = ""
    FakeSync.calls = []
    FakeSync.read_error = None
    FakeSync.upload_result = True
    FakeSync.hit_limit = False
    FakeSync.toggle_fails = set()
    FakeSync.toggle_raises = set()
    FakeSync.toggle_late = set()
    FakeSync.row_enabled = {}
    FakeScraper.filters = []
    monkeypatch.setattr(src.scraper.protonmail_sync, "ProtonMailSync", FakeSync)
    monkeypatch.setattr(src.scraper.protonmail_scraper, "ProtonMailScraper", FakeScraper)
    monkeypatch.setattr(src.main, "parse_scraped_filters", lambda raw: raw)
    return FakeSync


@pytest.fixture
def fake_scraper(fake_sync):
    return FakeScraper


@pytest.fixture
def shrunk_account(cli_snapshots_dir, fake_sync):
    """A shrunk account: 20 rules live in Sieve, only 2 UI filters left.

    Returns the live script. The latest snapshot holds a backup of the 2
    surviving filters (with the live script captured) and a consolidated.sieve
    generated from just those 2.
    """
    all_filters = [_filter(f"s{i}@x.com", folder=f"F{i % 3}") for i in range(20)]
    live = _section_for(all_filters)
    fake_sync.live_script = live

    manager = BackupManager(cli_snapshots_dir)
    manager.create_backup(all_filters[:2], sieve_script=live)
    FakeScraper.filters = all_filters[:2]
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
        assert ("disable", "Filter s0@x.com") in fake_sync.calls
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
        FakeScraper.filters = filters
        BackupManager(cli_snapshots_dir).create_backup(filters, sieve_script=fake_sync.live_script)
        assert runner.invoke(app, ["consolidate"]).exit_code == 0

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "No rules dropped" in result.output
        assert ("disable", "Filter a@x.com") in fake_sync.calls
        assert ("disable", "Filter b@x.com") in fake_sync.calls

    def test_first_sync_without_live_section_proceeds(self, cli_snapshots_dir, fake_sync):
        FakeScraper.filters = [_filter("a@x.com")]
        BackupManager(cli_snapshots_dir).create_backup([_filter("a@x.com")])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert ("disable", "Filter a@x.com") in fake_sync.calls


def test_dropped_listing_escapes_rich_markup(cli_snapshots_dir, fake_sync):
    """Subjects like "[SPAM]" must be printed literally, not eaten as markup."""
    spam = ProtonMailFilter(
        name="tagged",
        conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="[SPAM]")],
        actions=[FilterAction(type=ActionType.TRASH)],
    )
    fake_sync.live_script = _section_for([spam, _filter("a@x.com")])
    BackupManager(cli_snapshots_dir).create_backup([_filter("a@x.com")], sieve_script=fake_sync.live_script)
    assert runner.invoke(app, ["consolidate"]).exit_code == 0

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert '"[spam]"' in result.output


class TestCleanupOnlyDeletesCoveredFilters:

    def test_uncovered_disabled_filter_is_kept(self, cli_snapshots_dir, fake_sync, fake_scraper):
        covered = _filter("in-sieve@x.com", enabled=False)
        orphan = _filter("nowhere@x.com", enabled=False)
        fake_scraper.filters = [covered, orphan]
        fake_sync.live_script = _section_for([covered])
        BackupManager(cli_snapshots_dir).create_backup([covered, orphan])

        result = runner.invoke(app, ["cleanup"], input="y\n")
        # Exit 1: something was held back, though the covered one went
        assert result.exit_code == 1, result.output
        assert ("delete", covered.name) in fake_sync.calls
        assert ("delete", orphan.name) not in fake_sync.calls
        assert "NOT in the live" in result.output

    def test_no_live_section_deletes_nothing(self, cli_snapshots_dir, fake_sync, fake_scraper):
        fake_scraper.filters = [_filter("a@x.com", enabled=False)]
        fake_sync.live_script = ""
        BackupManager(cli_snapshots_dir).create_backup(fake_scraper.filters)

        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 1, result.output
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
        assert result.exit_code == 1
        assert fake_sync.calls == []


def _begins_with(prefix: str, enabled: bool = True) -> ProtonMailFilter:
    return ProtonMailFilter(
        name=f"Begins {prefix}",
        enabled=enabled,
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.STARTS_WITH, value=prefix)],
        actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "News"})],
        raw=ScrapeEvidence(conditions_text="the sender", actions_text="Move to News"),
    )


# A live section as older ProtonFusion versions generated it for "sender
# begins with news": :matches with no wildcard, i.e. an exact match.
_LEGACY_BEGINS_WITH_LIVE = SieveGenerator.merge_with_existing(
    'require ["fileinto"];\nif address :matches "From" "news" {\n    fileinto "News";\n}\n', "")


class TestLegacyWildcardLiveSection:

    def test_sync_reports_correction_and_proceeds(self, cli_snapshots_dir, fake_sync):
        fake_sync.live_script = _LEGACY_BEGINS_WITH_LIVE
        BackupManager(cli_snapshots_dir).create_backup(
            [_begins_with("news")], sieve_script=fake_sync.live_script)
        assert runner.invoke(app, ["consolidate"]).exit_code == 0

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "No rules dropped" in result.output
        assert "Corrected: 1 begins-with/ends-with" in result.output
        assert '"news*"' in result.output

    def test_cleanup_keeps_filter_whose_live_copy_is_the_old_form(
            self, cli_snapshots_dir, fake_sync, fake_scraper):
        """The old exact-match copy does not cover the filter, so it is not deleted."""
        disabled = _begins_with("news", enabled=False)
        fake_scraper.filters = [disabled]
        fake_sync.live_script = _LEGACY_BEGINS_WITH_LIVE
        BackupManager(cli_snapshots_dir).create_backup([disabled])

        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 1, result.output  # held back, so exit 1 (N2)
        assert ("delete", disabled.name) not in fake_sync.calls


def test_consolidate_refuses_filter_containing_section_marker(cli_snapshots_dir, fake_sync):
    from src.generator.sieve_generator import SECTION_END
    sneaky = ProtonMailFilter(
        name="Sneaky",
        conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS,
                                    value=f"x\n{SECTION_END}\ny")],
        actions=[FilterAction(type=ActionType.TRASH)],
    )
    BackupManager(cli_snapshots_dir).create_backup([sneaky])
    result = runner.invoke(app, ["consolidate"])
    assert result.exit_code == 1
    assert "Sneaky" in result.output
    assert "section marker" in result.output


class TestFailedLiveReadRefuses:
    """A failed read of the live script is never taken for "no script"."""

    @staticmethod
    def _fail(fake_sync):
        from src.scraper.browser import SieveReadError
        fake_sync.read_error = SieveReadError("the Sieve editor did not open")

    def test_sync_refuses_and_touches_nothing(self, shrunk_account, fake_sync):
        self._fail(fake_sync)
        result = runner.invoke(app, ["sync", "--allow-rule-removal"])
        assert result.exit_code == 1
        assert fake_sync.calls == []
        assert "Could not read the live Sieve script" in result.output
        assert "Nothing was changed" in result.output

    def test_show_diff_only_refuses(self, shrunk_account, fake_sync):
        self._fail(fake_sync)
        result = runner.invoke(app, ["sync", "--show-diff-only"])
        assert result.exit_code == 1
        assert "Could not read the live Sieve script" in result.output

    def test_cleanup_deletes_nothing_even_with_include_uncovered(
        self, cli_snapshots_dir, fake_sync, fake_scraper,
    ):
        orphan = _filter("nowhere@x.com", enabled=False)
        fake_scraper.filters = [orphan]
        BackupManager(cli_snapshots_dir).create_backup([orphan])
        self._fail(fake_sync)

        result = runner.invoke(app, ["cleanup", "--include-uncovered"], input="y\n")
        assert result.exit_code == 1
        assert fake_sync.calls == []
        assert "Could not read the live Sieve script" in result.output


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
        # (the generator now refuses a rule with no conditions, so add it by hand)
        from src.generator.sieve_generator import SECTION_END
        fake_sync.live_script = _section_for([covered]).replace(SECTION_END, f"keep;\n{SECTION_END}")
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
        assert result.exit_code == 1, result.output
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
        assert result.exit_code == 1, result.output
        assert fake_sync.calls == []
        assert "name is shared" in result.output

    def test_include_uncovered_does_not_override(self, cli_snapshots_dir, fake_sync, fake_scraper):
        a = self._named(_filter("a@x.com", enabled=False), "News")
        b = self._named(_filter("b@x.com", enabled=False), "News")
        fake_scraper.filters = [a, b]
        BackupManager(cli_snapshots_dir).create_backup([a, b])

        result = runner.invoke(app, ["cleanup", "--include-uncovered", "--allow-incomplete"], input="y\n")
        assert result.exit_code == 1, result.output
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
        assert any(c[0] == "upload" for c in fake_sync.calls)

    def test_explicit_other_script_not_checked(self, legacy_snapshot, fake_sync, tmp_path):
        """The manifest describes the snapshot's own script, not one given with --sieve."""
        other = tmp_path / "other.sieve"
        other.write_text(_section_for([_filter("new@x.com")]))
        result = runner.invoke(app, ["sync", "--sieve", str(other)])
        assert result.exit_code == 0, result.output


class TestSyncRefusesIncompleteSources:
    """sync refuses a script holding a rule from any filter not read in full (P2, sync half).

    Checked from the backup and archive against the script itself, so it
    holds for --sieve scripts and does not depend on the manifest. Scripts
    are generated from the complete twin of each filter, so the tests do not
    depend on what consolidate does with incomplete filters.
    """

    ISSUE = "condition 1: unknown condition type 'The size'"

    @pytest.fixture
    def script_with_incomplete(self, cli_snapshots_dir, fake_sync, tmp_path):
        """A backup with one incomplete filter, and a --sieve script holding its rule."""
        good, partial = _filter("good@x.com"), _filter("partial@x.com")
        BackupManager(cli_snapshots_dir).create_backup([
            good, partial.model_copy(update={"scrape_issues": [self.ISSUE]}),
        ])
        FakeScraper.filters = [good, partial]
        path = tmp_path / "s.sieve"
        path.write_text(_section_for([good, partial]))
        return str(path)

    def test_sync_refuses(self, script_with_incomplete, fake_sync):
        result = runner.invoke(app, ["sync", "--sieve", script_with_incomplete])
        assert result.exit_code == 1, result.output
        assert "- Filter partial@x.com" in result.output
        assert self.ISSUE in result.output
        assert "- Filter good@x.com" not in result.output
        assert "Sync refused" in result.output
        assert fake_sync.calls == []

    def test_dry_run_refuses(self, script_with_incomplete, fake_sync):
        result = runner.invoke(app, ["sync", "--dry-run", "--sieve", script_with_incomplete])
        assert result.exit_code == 1, result.output
        assert "Sync refused" in result.output

    def test_allow_incomplete_proceeds(self, script_with_incomplete, fake_sync):
        result = runner.invoke(app, ["sync", "--allow-incomplete", "--sieve", script_with_incomplete])
        assert result.exit_code == 0, result.output
        assert any(c[0] == "upload" for c in fake_sync.calls)

    def test_incomplete_filter_not_in_script_is_fine(self, cli_snapshots_dir, fake_sync, tmp_path):
        good, partial = _filter("good@x.com"), _filter("partial@x.com")
        BackupManager(cli_snapshots_dir).create_backup([
            good, partial.model_copy(update={"scrape_issues": [self.ISSUE]}),
        ])
        FakeScraper.filters = [good]
        path = tmp_path / "s.sieve"
        path.write_text(_section_for([good]))
        result = runner.invoke(app, ["sync", "--sieve", str(path)])
        assert result.exit_code == 0, result.output

    def test_incomplete_archived_filter_in_script_refuses(self, cli_snapshots_dir, fake_sync, tmp_path):
        from src.models.backup_models import ArchiveEntry
        good, partial = _filter("good@x.com"), _filter("partial@x.com")
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([good])
        snapshot_dir = manager.snapshot_dir_for("latest")
        manager.write_archive(snapshot_dir, [ArchiveEntry(
            filter=partial.model_copy(update={"scrape_issues": [self.ISSUE], "enabled": False}),
            archived_at="2026-01-01T00:00:00+00:00", source_snapshot=snapshot_dir.name,
            source_format="1.3",
        )])
        FakeScraper.filters = [good]
        path = tmp_path / "s.sieve"
        path.write_text(_section_for([good, partial]))
        result = runner.invoke(app, ["sync", "--sieve", str(path)])
        assert result.exit_code == 1, result.output
        assert "- Filter partial@x.com" in result.output


    @pytest.mark.parametrize("twin_state", ["disabled", "deprecated"])
    def test_switched_off_twin_does_not_vouch_without_manifest(
        self, cli_snapshots_dir, fake_sync, tmp_path, twin_state,
    ):
        """Hardening: with no manifest, a complete filter consolidate would not use
        (switched off, or deprecated in the archive) must not vouch for the
        incomplete filter's rule. Nothing says the script took the rule from it."""
        from src.models.backup_models import ArchiveEntry
        from src.models.filter_models import FilterStatus
        good, partial = _filter("good@x.com"), _filter("partial@x.com")
        twin = partial.model_copy(update={"name": "Twin of partial"})
        if twin_state == "disabled":
            twin = twin.model_copy(update={"enabled": False, "status": FilterStatus.DISABLED})
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([good, partial.model_copy(update={"scrape_issues": [self.ISSUE]}), twin])
        if twin_state == "deprecated":
            snapshot_dir = manager.snapshot_dir_for("latest")
            manager.write_archive(snapshot_dir, [ArchiveEntry(
                filter=twin.model_copy(update={"status": FilterStatus.DEPRECATED, "enabled": False}),
                source_snapshot=snapshot_dir.name, source_format="1.3",
            )])
        FakeScraper.filters = [good]
        path = tmp_path / "s.sieve"
        path.write_text(_section_for([good, partial]))
        result = runner.invoke(app, ["sync", "--dry-run", "--sieve", str(path)])
        assert result.exit_code == 1, result.output
        assert "- Filter partial@x.com" in result.output

    def test_enabled_twin_still_vouches_without_manifest(self, cli_snapshots_dir, fake_sync, tmp_path):
        """The rule is in the script on a filter consolidate would use, so the
        incomplete one is not blamed for it."""
        good, partial = _filter("good@x.com"), _filter("partial@x.com")
        twin = partial.model_copy(update={"name": "Twin of partial"})
        BackupManager(cli_snapshots_dir).create_backup([
            good, partial.model_copy(update={"scrape_issues": [self.ISSUE]}), twin,
        ])
        FakeScraper.filters = [good]
        path = tmp_path / "s.sieve"
        path.write_text(_section_for([good, partial]))
        result = runner.invoke(app, ["sync", "--dry-run", "--sieve", str(path)])
        assert result.exit_code == 0, result.output
        assert "not fully read when backed up" not in result.output


class TestSyncRefusesUnparsableMergedScript:
    """sync validates the merged script and refuses to upload one that does not parse."""

    BROKEN_USER_RULES = 'require ["fileinto"];\nif header :contains "Subject" "x" { fileinto "X";\n'

    @pytest.fixture
    def account(self, cli_snapshots_dir, fake_sync):
        f = _filter("a@x.com")
        BackupManager(cli_snapshots_dir).create_backup([f], sieve_script=self.BROKEN_USER_RULES)
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [f]
        fake_sync.live_script = self.BROKEN_USER_RULES

    def test_sync_refuses_and_touches_nothing(self, account, fake_sync):
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert "merged Sieve script does not parse" in result.output
        assert "Sync refused" in result.output
        assert fake_sync.calls == []

    def test_dry_run_reports_refusal(self, account, fake_sync):
        result = runner.invoke(app, ["sync", "--dry-run"])
        assert result.exit_code == 1, result.output
        assert "merged Sieve script does not parse" in result.output

    def test_show_diff_only_reports_refusal(self, account, fake_sync):
        result = runner.invoke(app, ["sync", "--show-diff-only"])
        assert result.exit_code == 1, result.output
        assert "merged Sieve script does not parse" in result.output
        assert fake_sync.calls == []


class TestSyncUnchangedScript:
    """When the live script already is the merged one, sync does not upload.

    Proton keeps Save disabled for an unchanged script, so an upload would
    be reported as failed. Sync still disables the replaced filters and
    makes sure ProtonFusion's own filter is on.
    """

    @pytest.fixture
    def account(self, cli_snapshots_dir, fake_sync):
        f = _filter("a@x.com")
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([f])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        generated = (manager.snapshot_dir_for("latest") / "consolidated.sieve").read_text()
        # Trailing whitespace only: still "the same script".
        fake_sync.live_script = SieveGenerator.merge_with_existing(generated, "") + "  \n\n"
        return f

    @staticmethod
    def _pf_row(enabled: bool) -> ProtonMailFilter:
        return ProtonMailFilter(
            name=SIEVE_FILTER_NAME, enabled=enabled, priority=1, is_sieve=True,
            raw=ScrapeEvidence(sieve_text="keep;"),
        )

    def test_no_upload_and_filters_still_disabled(self, account, fake_sync, cli_snapshots_dir):
        FakeScraper.filters = [account, self._pf_row(enabled=True)]
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert fake_sync.calls == [("disable", account.name)]
        assert "nothing to upload" in result.output
        assert "Sieve uploaded: No (already up to date)" in result.output
        # V8: the rollback command names the snapshot, not 'latest', which moves
        snapshot = BackupManager(cli_snapshots_dir).snapshot_dir_for("latest").name
        assert f"'restore --backup {snapshot}'" in result.output

    def test_switched_off_script_filter_is_switched_on(self, account, fake_sync):
        FakeScraper.filters = [account, self._pf_row(enabled=False)]
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert fake_sync.calls == [("disable", account.name), ("enable", SIEVE_FILTER_NAME)]

    def test_failed_switch_on_reenables_disabled_filters(self, account, fake_sync):
        FakeScraper.filters = [account, self._pf_row(enabled=False)]
        fake_sync.toggle_fails = {(SIEVE_FILTER_NAME, True)}
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert f"Failed to switch on the '{SIEVE_FILTER_NAME}' filter" in result.output
        assert fake_sync.calls == [("disable", account.name), ("enable", account.name)]

    def test_swallowed_switch_on_click_is_a_failure(self, account, fake_sync, monkeypatch):
        """V5: the real set_row_enabled, over a page whose ProtonFusion switch ignores clicks.

        Before the read-back, the click "succeeded", the UI filter stayed off
        and sync reported success with nothing filtering that mail.
        """
        import src.scraper.protonmail_sync
        from src.scraper import selectors
        from tests.test_toggle_row import TogglePage

        page = TogglePage([(account.name, True), (SIEVE_FILTER_NAME, False)], stuck={1})

        class PageSync(RealProtonMailSync):
            """ProtonMailSync with its browser steps stubbed and `page` as the filters page."""

            def __init__(self, *args, **kwargs):
                self.page = page

            async def initialize(self):
                pass

            async def login(self):
                return True

            async def navigate_to_filters(self):
                pass

            async def close(self):
                pass

        monkeypatch.setattr(src.scraper.protonmail_sync, "ProtonMailSync", PageSync)
        FakeScraper.filters = [account, self._pf_row(enabled=False)]
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert f"Failed to switch on the '{SIEVE_FILTER_NAME}' filter" in result.output
        assert "Re-enabled 1 of the 1 filters this sync disabled" in result.output
        assert "active-filter limit" in result.output
        toggles = [row.children[selectors.FILTER_TOGGLE].checked for row in page.rows]
        assert toggles == [True, False]

    def test_script_filter_not_found_refuses(self, account, fake_sync):
        FakeScraper.filters = [account]
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert "Sync refused" in result.output
        assert fake_sync.calls == []


class TestSyncKeepsUserVacationRule:
    """A live script with a text: literal (vacation rule) merges, validates and uploads."""

    VACATION = (
        'require ["vacation"];\n'
        'if header :contains "Subject" "hello" {\n'
        '  vacation :days 1 text:\nAway until Monday.\n.\n;\n'
        '}\n'
    )

    def test_sync_uploads_with_vacation_rule_kept(self, cli_snapshots_dir, fake_sync):
        f = _filter("a@x.com")
        BackupManager(cli_snapshots_dir).create_backup([f], sieve_script=self.VACATION)
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [f]
        fake_sync.live_script = self.VACATION
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        (uploaded,) = [arg for op, arg in fake_sync.calls if op == "upload"]
        assert "Away until Monday." in uploaded
        assert '"vacation"' in uploaded


@pytest.mark.parametrize("script,ok", [
    ('require ["fileinto"];\nif header :is "From" "a" { fileinto "X"; }', True),
    ('require "fileinto";\nrequire ["imap4flags"];\nkeep;', True),
    ('keep;\nrequire ["fileinto"];', False),
    ('if header :is "From" "a" { fileinto "X";', False),
    ('', True),
    # A user vacation rule: multi-line text: literals are valid Sieve.
    ('require ["vacation"];\nvacation :days 1 text:\nAway until Monday.\n.\n;\n', True),
    # fileinto used without being required
    ('if header :is "From" "a" { fileinto "X"; }', False),
])
def test_merged_script_problem(script, ok):
    from src.main import _merged_script_problem
    assert (_merged_script_problem(script) is None) is ok


class TestCleanupExitCode:
    """cleanup exits 1 whenever it kept back anything it would otherwise delete."""

    def test_everything_deleted_exits_0(self, cli_snapshots_dir, fake_sync, fake_scraper):
        covered = _filter("in-sieve@x.com", enabled=False)
        fake_scraper.filters = [covered]
        fake_sync.live_script = _section_for([covered])
        BackupManager(cli_snapshots_dir).create_backup([covered])
        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 0, result.output

    def test_dry_run_with_uncovered_exits_1(self, cli_snapshots_dir, fake_sync, fake_scraper):
        covered = _filter("in-sieve@x.com", enabled=False)
        orphan = _filter("nowhere@x.com", enabled=False)
        fake_scraper.filters = [covered, orphan]
        fake_sync.live_script = _section_for([covered])
        BackupManager(cli_snapshots_dir).create_backup([covered, orphan])
        result = runner.invoke(app, ["cleanup", "--dry-run"])
        assert result.exit_code == 1, result.output
        assert fake_sync.calls == []

    def test_failed_delete_exits_1(self, cli_snapshots_dir, fake_sync, fake_scraper, monkeypatch):
        covered = _filter("in-sieve@x.com", enabled=False)
        fake_scraper.filters = [covered]
        fake_sync.live_script = _section_for([covered])
        BackupManager(cli_snapshots_dir).create_backup([covered])

        async def refuse(self, name):
            return False
        monkeypatch.setattr(FakeSync, "delete_filter", refuse)
        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 1, result.output
        assert "1 filter(s) were not deleted" in result.output


class TestTruncatedLiveSection:
    """A live script with BEGIN but no END must refuse everywhere, never crash or pass."""

    @staticmethod
    def _truncate(script: str) -> str:
        from src.generator.sieve_generator import SECTION_END
        return script.replace(SECTION_END, "")

    def test_sync_refuses(self, shrunk_account, fake_sync):
        fake_sync.live_script = self._truncate(shrunk_account)
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "BEGIN marker but no END" in result.output
        assert fake_sync.calls == []

    def test_cleanup_deletes_nothing(self, cli_snapshots_dir, fake_sync, fake_scraper):
        covered = _filter("in-sieve@x.com", enabled=False)
        fake_scraper.filters = [covered]
        fake_sync.live_script = self._truncate(_section_for([covered]))
        BackupManager(cli_snapshots_dir).create_backup([covered])
        result = runner.invoke(app, ["cleanup"], input="y\n")
        assert result.exit_code == 1, result.output
        assert "Could not parse" in result.output
        assert fake_sync.calls == []

    def test_consolidate_warns(self, cli_snapshots_dir, fake_sync):
        f = _filter("a@x.com")
        BackupManager(cli_snapshots_dir).create_backup([f], sieve_script=self._truncate(_section_for([f])))
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Could not parse the ProtonFusion section" in result.output


class TestSyncDisablesOnlyReplacedFilters:
    """sync disables only wizard filters whose rules are in the uploaded script (S1)."""

    @staticmethod
    def _sieve(name: str) -> ProtonMailFilter:
        return ProtonMailFilter(name=name, is_sieve=True, raw=ScrapeEvidence(sieve_text="keep;"))

    @pytest.fixture
    def account(self, cli_snapshots_dir, fake_sync):
        """Backup of two wizard filters and two Sieve filters, consolidated.

        The live account also has a filter created after the backup and one
        edited since (same name, different sender). Returns the backed-up
        wizard filters.
        """
        backed_up = [_filter("a@x.com"), _filter("b@x.com")]
        sieves = [self._sieve(SIEVE_FILTER_NAME), self._sieve("Hand-written")]
        BackupManager(cli_snapshots_dir).create_backup(backed_up + sieves)
        assert runner.invoke(app, ["consolidate"]).exit_code == 0

        edited = _filter("b-changed@x.com").model_copy(update={"name": "Filter b@x.com"})
        FakeScraper.filters = [backed_up[0], edited, *sieves, _filter("new@x.com")]
        return backed_up

    @staticmethod
    def _toggled(fake_sync, verb):
        return [c[1] for c in fake_sync.calls if c[0] == verb]

    def test_only_backed_up_wizard_filters_are_disabled(self, account, fake_sync):
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert self._toggled(fake_sync, "disable") == ["Filter a@x.com"]
        # New and edited filters stay on, and are named as such
        assert "2 created or changed after backup 'latest' were left enabled" in result.output
        assert "Filter new@x.com" in result.output
        assert "Run 'backup' and 'consolidate' to fold them in" in result.output
        assert "2 Sieve filters were left enabled" in result.output

    def test_sieve_filters_never_disabled(self, account, fake_sync):
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        disabled = self._toggled(fake_sync, "disable")
        assert SIEVE_FILTER_NAME not in disabled
        assert "Hand-written" not in disabled

    def test_excluded_filter_left_enabled(self, cli_snapshots_dir, fake_sync):
        kept, excluded = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([kept, excluded])
        assert runner.invoke(app, ["consolidate", "--exclude", excluded.name]).exit_code == 0
        FakeScraper.filters = [kept, excluded]

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert self._toggled(fake_sync, "disable") == [kept.name]
        assert "left out of this script" in result.output

    def test_unreadable_row_left_enabled(self, cli_snapshots_dir, fake_sync):
        f = _filter("a@x.com")
        BackupManager(cli_snapshots_dir).create_backup([f])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [f.model_copy(update={"scrape_issues": ["condition 0: no value found"]})]

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert self._toggled(fake_sync, "disable") == []
        assert "could not be read in full" in result.output

    @pytest.mark.parametrize("failure", [False, RuntimeError("editor crashed")])
    def test_failed_upload_reenables_exactly_what_was_disabled(self, account, fake_sync, failure):
        fake_sync.upload_result = failure
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert self._toggled(fake_sync, "enable") == self._toggled(fake_sync, "disable") == ["Filter a@x.com"]
        assert "Re-enabled 1 of the 1 filters" in result.output
        assert "Could not re-enable" not in result.output

    def test_upload_error_printed_without_url_secrets(self, account, fake_sync):
        fake_sync.upload_result = RuntimeError('navigated to "https://account.proton.me/x#sk=SECRET"')
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert "Failed to upload Sieve script" in result.output
        assert "account.proton.me" in result.output
        assert "SECRET" not in result.output

    def test_filter_limit_named_on_failure(self, account, fake_sync):
        fake_sync.upload_result = False
        fake_sync.hit_limit = True
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert "active-filter limit" in result.output
        # Never falls back to disabling everything
        assert self._toggled(fake_sync, "disable") == ["Filter a@x.com"]

    def test_partial_reenable_failure_reported(self, cli_snapshots_dir, fake_sync):
        a, b = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [a, b]
        fake_sync.upload_result = False
        fake_sync.toggle_fails = {(b.name, True)}

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert self._toggled(fake_sync, "enable") == [a.name]
        assert "Re-enabled 1 of the 2 filters" in result.output
        assert "Could not re-enable 1 filter(s)" in result.output
        assert f"- {b.name}" in result.output
        # V8: the resolved snapshot, since the next backup moves 'latest'
        snapshot = BackupManager(cli_snapshots_dir).snapshot_dir_for("latest").name
        assert f"restore --backup {snapshot}" in result.output
        assert "restore --backup latest" not in result.output

    def test_reenable_continues_after_exception(self, cli_snapshots_dir, fake_sync):
        a, b, c = _filter("a@x.com"), _filter("b@x.com"), _filter("c@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b, c])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [a, b, c]
        fake_sync.upload_result = False
        fake_sync.toggle_raises = {(a.name, True)}

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        # The exception on a's row did not stop b and c being re-enabled
        assert self._toggled(fake_sync, "enable") == [b.name, c.name]
        assert "Re-enabled 2 of the 3 filters" in result.output
        assert "Re-enabled 3 of the 3" not in result.output
        assert f"- {a.name}: row for {a.name} detached" in result.output

    def test_unconfirmed_disable_is_reenabled_after_failed_upload(self, cli_snapshots_dir, fake_sync):
        """W8: b's switch went off only after the read-back gave up. It may be off, so a
        failed upload switches it back on with the rest."""
        a, b = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [a, b]
        fake_sync.toggle_late = {(b.name, False)}
        fake_sync.upload_result = False

        result = runner.invoke(app, ["sync"])
        flat = " ".join(result.output.split())
        assert result.exit_code == 1, result.output
        assert self._toggled(fake_sync, "enable") == [a.name, b.name]
        assert "Could not confirm 1 filter(s) off" in flat
        assert "Re-enabled 2 of the 2 filters" in flat

    def test_failed_upload_does_not_enable_user_disabled_filter(self, cli_snapshots_dir, fake_sync):
        """A filter the user switched off is never disabled by sync, so never re-enabled."""
        on, off = _filter("on@x.com"), _filter("off@x.com")
        BackupManager(cli_snapshots_dir).create_backup([on, off])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [on, off.model_copy(update={"enabled": False})]
        fake_sync.upload_result = False

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert self._toggled(fake_sync, "disable") == [on.name]
        assert self._toggled(fake_sync, "enable") == [on.name]

    def test_row_switched_off_since_scrape_is_not_reenabled(self, cli_snapshots_dir, fake_sync):
        """Scraped enabled, but off by the time sync clicks: left alone, never re-enabled."""
        a, b = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [a, b]
        fake_sync.row_enabled = {b.name: False}
        fake_sync.upload_result = False

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert self._toggled(fake_sync, "disable") == [a.name]
        assert self._toggled(fake_sync, "enable") == [a.name]

    def test_exception_while_disabling_restores_and_uploads_nothing(self, cli_snapshots_dir, fake_sync):
        a, b = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        FakeScraper.filters = [a, b]
        fake_sync.toggle_raises = {(b.name, False)}

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert "Sync stopped while disabling" in result.output
        assert not any(c[0] == "upload" for c in fake_sync.calls)
        assert self._toggled(fake_sync, "enable") == [a.name, b.name]

    @pytest.fixture
    def b_excluded(self, cli_snapshots_dir, fake_sync):
        """Backup of a and b; the snapshot's own consolidated.sieve leaves b out."""
        a, b = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b])
        assert runner.invoke(app, ["consolidate", "--exclude", b.name]).exit_code == 0
        FakeScraper.filters = [a, b]
        return a, b

    @staticmethod
    def _assert_b_kept(fake_sync, result, a, b):
        assert result.exit_code == 0, result.output
        assert TestSyncDisablesOnlyReplacedFilters._toggled(fake_sync, "disable") == [a.name]
        assert "not all in the script being uploaded" in result.output
        assert f"- {b.name}" in result.output

    def test_second_consolidate_output_does_not_disable_uncarried(self, b_excluded, fake_sync, tmp_path):
        """The manifest now names the --output file, so the snapshot's script falls back to the backup."""
        a, b = b_excluded
        assert runner.invoke(app, ["consolidate", "--output", str(tmp_path / "all.sieve")]).exit_code == 0
        self._assert_b_kept(fake_sync, runner.invoke(app, ["sync"]), a, b)

    def test_relative_manifest_path_from_other_directory(
        self, cli_snapshots_dir, fake_sync, tmp_path, monkeypatch,
    ):
        """consolidate stores the resolved path, so a relative --output still matches from elsewhere."""
        a, b = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b])
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        assert runner.invoke(app, ["consolidate", "--exclude", b.name, "--output", "out.sieve"]).exit_code == 0
        FakeScraper.filters = [a, b]
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        result = runner.invoke(app, ["sync", "--sieve", str(work / "out.sieve")])
        assert result.exit_code == 0, result.output
        assert "No consolidate manifest describes this script" not in result.output
        assert self._toggled(fake_sync, "disable") == [a.name]

    def test_relative_manifest_path_same_directory_uses_manifest(
        self, cli_snapshots_dir, fake_sync, tmp_path, monkeypatch,
    ):
        a, b = _filter("a@x.com"), _filter("b@x.com")
        BackupManager(cli_snapshots_dir).create_backup([a, b])
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["consolidate", "--exclude", b.name, "--output", "out.sieve"]).exit_code == 0
        FakeScraper.filters = [a, b]
        result = runner.invoke(app, ["sync", "--sieve", str(tmp_path / "out.sieve")])
        assert result.exit_code == 0, result.output
        assert "No consolidate manifest describes this script" not in result.output
        assert self._toggled(fake_sync, "disable") == [a.name]

    def test_sieve_copy_does_not_disable_uncarried(self, b_excluded, fake_sync, cli_snapshots_dir, tmp_path):
        a, b = b_excluded
        copy = tmp_path / "copy.sieve"
        copy.write_text((cli_snapshots_dir / "latest" / "consolidated.sieve").read_text())
        self._assert_b_kept(fake_sync, runner.invoke(app, ["sync", "--sieve", str(copy)]), a, b)

    def test_dry_run_lists_plan(self, account, fake_sync):
        result = runner.invoke(app, ["sync", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Would disable 2 UI filters" in result.output
        assert "2 Sieve filters would be left enabled" in result.output
        assert "--show-diff-only lists them" in result.output
        assert fake_sync.calls == []

    def test_show_diff_only_lists_plan_from_live_account(self, account, fake_sync):
        result = runner.invoke(app, ["sync", "--show-diff-only"])
        assert result.exit_code == 0, result.output
        assert "Would disable 1 UI filters" in result.output
        assert "2 created or changed after backup 'latest' would be left enabled" in result.output
        assert fake_sync.calls == []


def test_misread_rule_from_old_snapshot_does_not_survive_rebackup(cli_snapshots_dir, fake_sync):
    """V1: consolidating an old (pre-1.3) backup must not archive its possibly
    misread filters, or the fresh backup the old-snapshot warning asks for
    carries them forward and sync uploads the misread rule.

    The old scraper read "Subject does not contain 'receipt' -> Trash" as
    "contains", stored complete with evidence.
    """
    import json
    from src.backup.backup_manager import compute_checksum
    misread = ProtonMailFilter(
        name="Trash non-receipts from shop", logic="and",
        conditions=[{"type": "subject", "operator": "contains", "value": "receipt"}],
        actions=[{"type": "trash"}], raw={"conditions_text": "does not contain receipt", "actions_text": "Trash"})
    old_dir = cli_snapshots_dir / "2026-09-01_00-00-00"
    old_dir.mkdir()
    (old_dir / "backup.json").write_text(json.dumps({
        "version": "1.2", "timestamp": "2026-09-01T00:00:00", "metadata": {"filter_count": 1},
        "filters": [misread.model_dump(mode="json")], "sieve_script": "",
        "checksum": compute_checksum([misread], "", "1.2"),
    }))
    (cli_snapshots_dir / "latest").symlink_to(old_dir.name)

    result = runner.invoke(app, ["consolidate"])
    assert "Old Snapshot" in result.output
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1 and "--allow-old-snapshot" in result.output

    # The user follows the advice: backup again. The strict scraper flags the operator.
    correct = ProtonMailFilter(
        name="Trash non-receipts from shop", logic="and", conditions=[],
        actions=[{"type": "trash"}], raw={"conditions_text": "does not contain receipt", "actions_text": "Trash"},
        scrape_issues=["condition 1: unknown operator 'does not contain'"])
    BackupManager(cli_snapshots_dir).create_backup([correct])
    result = runner.invoke(app, ["consolidate"])
    assert result.exit_code == 0, result.output
    script = (cli_snapshots_dir / "latest" / "consolidated.sieve").read_text()
    assert '"receipt"' not in script
    FakeScraper.filters = [correct]
    result = runner.invoke(app, ["sync"])
    uploads = [c[1] for c in FakeSync.calls if c[0] == "upload"]
    assert not any('"receipt"' in u for u in uploads), result.output


class TestLeftOutIncompleteFilterDoesNotBlockSync:
    """V6: an incomplete filter consolidate left out of the script does not block
    sync because what was read of it equals a complete filter's rule."""

    @pytest.fixture
    def account(self, cli_snapshots_dir, fake_sync):
        complete = _filter("a@x.com", folder="News")
        half_read = ProtonMailFilter(**{
            **complete.model_dump(), "name": "News from a, no receipts",
            "scrape_issues": ["condition 2: unknown operator 'does not contain'"],
        })
        BackupManager(cli_snapshots_dir).create_backup([complete, half_read])
        FakeScraper.filters = [complete, half_read]
        return cli_snapshots_dir

    def test_sync_proceeds_with_manifest(self, account, fake_sync):
        result = runner.invoke(app, ["consolidate"])
        assert "Left out of the script: 1" in result.output
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "not fully read when backed up" not in result.output
        assert any(c[0] == "upload" for c in fake_sync.calls)
        # Left enabled, as consolidate said
        assert ("disable", "News from a, no receipts") not in fake_sync.calls

    def test_sync_proceeds_without_manifest(self, account, fake_sync, tmp_path):
        path = tmp_path / "s.sieve"
        path.write_text(_section_for([_filter("a@x.com", folder="News")]))
        result = runner.invoke(app, ["sync", "--sieve", str(path)])
        assert result.exit_code == 0, result.output

    def test_sync_still_refuses_when_included(self, account, fake_sync):
        assert runner.invoke(app, ["consolidate", "--allow-incomplete"]).exit_code == 0
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert "not fully read when backed up" in result.output
        assert "- News from a, no receipts" in result.output
