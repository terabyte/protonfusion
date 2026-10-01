"""Tests for `restore`: it matches filters by content and position, never by name alone (P12).

Offline: the browser classes are replaced with fakes.
"""

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.utils.config
from src.backup.backup_manager import BackupManager
from src.backup.restore_engine import RestoreEngine
from src.main import app, _rollback_help, SIEVE_FILTER_NAME
from src.models.backup_models import Backup
from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, ConditionType, Operator, ActionType,
    ScrapeEvidence,
)

runner = CliRunner()


def _filter(name, sender, enabled=True, priority=0, action=ActionType.LABEL) -> ProtonMailFilter:
    params = {"label": "Work"} if action == ActionType.LABEL else {}
    return ProtonMailFilter(
        name=name, enabled=enabled, priority=priority,
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value=sender)],
        actions=[FilterAction(type=action, parameters=params)],
        raw=ScrapeEvidence(conditions_text="c", actions_text="a"),
    )


def _sieve(name, script, enabled=True, priority=0) -> ProtonMailFilter:
    return ProtonMailFilter(
        name=name, enabled=enabled, priority=priority, raw=ScrapeEvidence(sieve_text=script),
    )


class FakeSync:
    """Records set_row_enabled calls; the other toggles must never be used."""

    last_toggle_refused = False

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    async def set_row_enabled(self, index, name, enabled):
        if (index, name) in self.fail:
            return False
        self.calls.append((index, name, enabled))
        return True

    async def enable_filter(self, name):
        raise AssertionError("restore must not toggle by name alone")

    async def disable_filter(self, name):
        raise AssertionError("restore must not toggle by name alone")


def _restore(backed_up, current, sync=None):
    sync = sync or FakeSync()
    report = asyncio.run(RestoreEngine(sync).restore_from_backup(Backup(filters=backed_up), current))
    return report, sync


class TestRestoreEngine:

    def test_toggles_matching_row_by_position(self):
        report, sync = _restore(
            [_filter("A", "a@x", enabled=True, priority=0)],
            [_filter("A", "a@x", enabled=False, priority=3)],
        )
        assert sync.calls == [(3, "A", True)]
        assert report["enabled"] == ["A"]

    def test_duplicate_name_restores_the_right_row(self):
        """Two "News" filters with different rules: each is restored to its own state.

        By name, the first "News" row would have taken both toggles."""
        backed_up = [
            _filter("News", "keep@x", enabled=True, priority=0),
            _filter("News", "drop@x", enabled=False, priority=1, action=ActionType.TRASH),
        ]
        # Since the backup, a sync disabled the first and someone enabled the second
        current = [
            _filter("News", "keep@x", enabled=False, priority=0),
            _filter("News", "drop@x", enabled=True, priority=1, action=ActionType.TRASH),
        ]
        report, sync = _restore(backed_up, current)
        assert sorted(sync.calls) == [(0, "News", True), (1, "News", False)]
        assert report["enabled"] == ["News"] and report["disabled"] == ["News"]

    def test_changed_filter_not_restored(self):
        """Same name, different rules: not the same filter, so not touched."""
        report, sync = _restore(
            [_filter("A", "a@x", enabled=True)],
            [_filter("A", "other@x", enabled=False)],
        )
        assert sync.calls == []
        assert report["not_found"] == ["A: not in the account, or changed since the backup"]

    def test_identical_copies_paired_in_row_order(self):
        backed_up = [_filter("T", "t@x", enabled=True, priority=0), _filter("T", "t@x", enabled=False, priority=1)]
        current = [_filter("T", "t@x", enabled=False, priority=4), _filter("T", "t@x", enabled=False, priority=5)]
        report, sync = _restore(backed_up, current)
        assert sync.calls == [(4, "T", True)]

    def test_count_mismatch_is_ambiguous(self):
        backed_up = [_filter("T", "t@x", enabled=True)]
        current = [_filter("T", "t@x", enabled=False, priority=0), _filter("T", "t@x", enabled=False, priority=1)]
        report, sync = _restore(backed_up, current)
        assert sync.calls == []
        assert len(report["ambiguous"]) == 1 and "cannot tell which is which" in report["ambiguous"][0]

    def test_sieve_filter_matched_by_name_and_script_change_reported(self):
        report, sync = _restore(
            [_sieve(SIEVE_FILTER_NAME, "keep;", enabled=False, priority=0)],
            [_sieve(SIEVE_FILTER_NAME, "discard;", enabled=True, priority=2)],
        )
        assert sync.calls == [(2, SIEVE_FILTER_NAME, False)]
        assert report["script_not_restored"] == [SIEVE_FILTER_NAME]

    def test_duplicate_sieve_names_are_ambiguous(self):
        report, sync = _restore(
            [_sieve("S", "keep;", enabled=False), _sieve("S", "stop;", enabled=False, priority=1)],
            [_sieve("S", "keep;", enabled=True), _sieve("S", "stop;", enabled=True, priority=1)],
        )
        assert sync.calls == []
        assert len(report["ambiguous"]) == 2

    def test_failed_toggle_reported(self):
        report, sync = _restore(
            [_filter("A", "a@x", enabled=True)],
            [_filter("A", "a@x", enabled=False, priority=0)],
            sync=FakeSync(fail={(0, "A")}),
        )
        assert report["errors"] and "failed to enable" in report["errors"][0]

    def test_switch_that_ignores_the_click_is_an_error(self):
        """V5: the real set_row_enabled, over a page whose switch does not change on click."""
        from src.scraper.protonmail_sync import ProtonMailSync
        from tests.test_toggle_row import TogglePage
        sync = ProtonMailSync()
        sync.page = TogglePage([("A", False)], stuck={0})
        report = asyncio.run(RestoreEngine(sync).restore_from_backup(
            Backup(filters=[_filter("A", "a@x", enabled=True)]),
            [_filter("A", "a@x", enabled=False, priority=0)],
        ))
        assert report["enabled"] == []
        assert report["errors"] == ["A: failed to enable: its switch did not change when clicked"]


def _section_for(filters) -> str:
    """A full live script whose ProtonFusion section holds `filters`."""
    from src.consolidator.consolidation_engine import ConsolidationEngine
    from src.generator.sieve_generator import SieveGenerator
    consolidated, _ = ConsolidationEngine().consolidate(filters, include_disabled=True)
    return SieveGenerator.merge_with_existing(SieveGenerator().generate(consolidated), "")


OLD_SCRIPT = _section_for([_filter("Old", "old@x")])
NEW_SCRIPT = _section_for([_filter("Old", "old@x"), _filter("New", "new@x")])


class FakeBrowser:
    """Stands in for both ProtonMailScraper and ProtonMailSync; records every write in order."""
    current: list = []
    live_script = ""
    read_error = None
    upload_result = True  # False, or an exception instance to raise
    toggle_fails: set = set()  # (name, enabled) pairs set_row_enabled refuses
    last_toggle_refused = False
    calls: list = []
    account_email = "test@proton.me"

    def __init__(self, *args, **kwargs):
        pass

    async def initialize(self):
        pass

    async def login(self):
        pass

    async def navigate_to_filters(self):
        pass

    async def scrape_all_filters(self, workers=1):
        return list(FakeBrowser.current)

    async def read_sieve_script(self, filter_name=""):
        if FakeBrowser.read_error:
            raise FakeBrowser.read_error
        return FakeBrowser.live_script

    async def set_row_enabled(self, index, name, enabled):
        if (name, enabled) in FakeBrowser.toggle_fails:
            return False
        FakeBrowser.calls.append(("enable" if enabled else "disable", name))
        return True

    async def upload_sieve(self, script, filter_name=""):
        FakeBrowser.calls.append(("upload", script, filter_name))
        if isinstance(FakeBrowser.upload_result, Exception):
            raise FakeBrowser.upload_result
        return FakeBrowser.upload_result

    async def close(self):
        pass


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    import src.main
    import src.backup.backup_manager
    import src.scraper.protonmail_scraper
    import src.scraper.protonmail_sync
    from rich.console import Console
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", snapshots_dir)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", snapshots_dir)
    monkeypatch.setattr(src.main, "console", Console(width=400))
    monkeypatch.setattr(src.scraper.protonmail_scraper, "ProtonMailScraper", FakeBrowser)
    monkeypatch.setattr(src.scraper.protonmail_sync, "ProtonMailSync", FakeBrowser)
    monkeypatch.setattr(src.main, "parse_scraped_filters", lambda raw: raw)
    FakeBrowser.current = []
    FakeBrowser.live_script = ""
    FakeBrowser.read_error = None
    FakeBrowser.upload_result = True
    FakeBrowser.toggle_fails = set()
    FakeBrowser.calls = []
    return snapshots_dir


def _snapshot_dirs(snapshots_dir):
    return sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir() and not p.is_symlink())


@pytest.fixture
def after_sync(cli_env):
    """The account after a sync: the backup (taken before it) had the UI filter
    "Old" on and ProtonFusion's script OLD_SCRIPT; the sync disabled "Old",
    added "New" to the script, and someone since switched "Spare" on."""
    BackupManager(cli_env).create_backup([
        _filter("Old", "old@x", enabled=True, priority=0),
        _filter("Spare", "spare@x", enabled=False, priority=1),
        _sieve(SIEVE_FILTER_NAME, OLD_SCRIPT, enabled=True, priority=2),
    ], sieve_script=OLD_SCRIPT)
    FakeBrowser.current = [
        _filter("Old", "old@x", enabled=False, priority=0),
        _filter("Spare", "spare@x", enabled=True, priority=1),
        _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=2),
    ]
    FakeBrowser.live_script = NEW_SCRIPT
    return cli_env


class TestRestoreCommand:
    """D2: restore is a full rollback, previewed, confirmed, and undoable."""

    def test_full_rollback_in_safe_order(self, after_sync):
        """Enable first, then the script, then disable: never a rule switched off early."""
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [
            ("enable", "Old"),
            ("upload", OLD_SCRIPT, SIEVE_FILTER_NAME),
            ("disable", "Spare"),
        ]
        assert "Restore complete." in result.output

    def test_unchanged_script_is_not_uploaded(self, after_sync):
        """Proton keeps Save disabled for an unchanged script; trailing whitespace is no change."""
        FakeBrowser.live_script = OLD_SCRIPT + "  \n\n"
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [("enable", "Old"), ("disable", "Spare")]
        assert "Sieve script: already as in the backup" in result.output

    def test_safety_backup_taken_and_named(self, after_sync):
        latest_before = (after_sync / "latest").resolve()
        dirs_before = _snapshot_dirs(after_sync)
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        [safety_id] = sorted(set(_snapshot_dirs(after_sync)) - set(dirs_before))
        assert f"restore --backup {safety_id}" in result.output
        safety = BackupManager(after_sync).load_backup(safety_id)
        assert safety.sieve_script == NEW_SCRIPT
        assert [f.enabled for f in safety.filters] == [False, True, True]
        # It describes the pre-restore account, so it does not become 'latest'
        assert (after_sync / "latest").resolve() == latest_before

    def test_preview_shows_changes_and_diff(self, after_sync):
        result = runner.invoke(app, ["restore", "--backup", "latest", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Will enable 1 filter(s)" in result.output and "- Old (row 0)" in result.output
        assert "Will disable 1 filter(s)" in result.output and "- Spare (row 1)" in result.output
        assert "1 condition/action pairs removed, 0 added" in result.output
        assert '-if address :is "From" ["old@x", "new@x"] {' in result.output
        assert '+if address :is "From" "old@x" {' in result.output

    def test_dry_run_changes_and_saves_nothing(self, after_sync):
        dirs_before = _snapshot_dirs(after_sync)
        result = runner.invoke(app, ["restore", "--backup", "latest", "--dry-run"])
        assert "DRY RUN - nothing was changed" in result.output
        assert FakeBrowser.calls == []
        assert _snapshot_dirs(after_sync) == dirs_before

    def test_declined_changes_and_saves_nothing(self, after_sync):
        dirs_before = _snapshot_dirs(after_sync)
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="n\n")
        assert "Restore cancelled. Nothing was changed." in result.output
        assert FakeBrowser.calls == []
        assert _snapshot_dirs(after_sync) == dirs_before

    def test_failed_live_read_refuses(self, after_sync):
        from src.scraper.browser import SieveReadError
        FakeBrowser.read_error = SieveReadError("editor did not open")
        dirs_before = _snapshot_dirs(after_sync)
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1
        assert "Could not read the live Sieve script" in result.output
        assert FakeBrowser.calls == []
        assert _snapshot_dirs(after_sync) == dirs_before

    def test_failed_upload_stops_before_disabling(self, after_sync):
        FakeBrowser.upload_result = False
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1
        assert FakeBrowser.calls == [("enable", "Old"), ("upload", OLD_SCRIPT, SIEVE_FILTER_NAME)]
        assert "Restore did NOT complete." in result.output
        assert "unchanged: the upload did not complete" in result.output
        assert "not attempted" in result.output
        assert "no mail is left unfiltered" in result.output

    def test_upload_exception_reports_unknown_script_state(self, after_sync):
        FakeBrowser.upload_result = RuntimeError("page closed")
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1
        assert "UNKNOWN" in result.output and "page closed" in result.output
        assert ("disable", "Spare") not in FakeBrowser.calls

    def test_failed_enable_stops_before_upload(self, after_sync):
        FakeBrowser.toggle_fails = {("Old", True)}
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1
        assert FakeBrowser.calls == []
        assert "Could not enable" in result.output and "- Old" in result.output

    def test_backup_without_script_refuses_without_flag(self, cli_env):
        BackupManager(cli_env).create_backup([_filter("Old", "old@x", enabled=True)], sieve_script="")
        FakeBrowser.current = [
            _filter("Old", "old@x", enabled=False, priority=0),
            _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=1),
        ]
        FakeBrowser.live_script = NEW_SCRIPT
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1
        assert "holds no Sieve script" in result.output
        assert "--allow-empty-script" in result.output
        assert FakeBrowser.calls == []

        FakeBrowser.calls = []
        result = runner.invoke(app, ["restore", "--backup", "latest", "--allow-empty-script"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [("enable", "Old"), ("disable", SIEVE_FILTER_NAME)]
        assert f"'{SIEVE_FILTER_NAME}' disabled" in result.output

    def test_backup_predating_protonfusion_section(self, cli_env):
        """A backed-up script with no ProtonFusion section: restoring it removes the section."""
        user_script = 'require ["fileinto"];\nif header :contains "subject" "x" { fileinto "X"; }\n'
        BackupManager(cli_env).create_backup(
            [_sieve(SIEVE_FILTER_NAME, user_script)], sieve_script=user_script,
        )
        FakeBrowser.current = [_sieve(SIEVE_FILTER_NAME, NEW_SCRIPT)]
        FakeBrowser.live_script = NEW_SCRIPT
        result = runner.invoke(app, ["restore", "--backup", "latest", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "REMOVES ProtonFusion's" in result.output
        assert "section (2 condition/action pairs)" in result.output

    def test_backed_up_disabled_protonfusion_filter_switched_off_after_upload(self, cli_env):
        """Saving a script switches the filter on; the backup had it off, so it ends off."""
        BackupManager(cli_env).create_backup(
            [_sieve(SIEVE_FILTER_NAME, OLD_SCRIPT, enabled=False)], sieve_script=OLD_SCRIPT,
        )
        FakeBrowser.current = [_sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=False)]
        FakeBrowser.live_script = NEW_SCRIPT
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [
            ("upload", OLD_SCRIPT, SIEVE_FILTER_NAME),
            ("disable", SIEVE_FILTER_NAME),
        ]

    def test_reports_what_it_could_not_restore_and_exits_1(self, cli_env):
        BackupManager(cli_env).create_backup(
            [_filter("A", "a@x", enabled=True), _filter("Gone", "g@x", enabled=True, priority=1)],
        )
        FakeBrowser.current = [_filter("A", "a@x", enabled=False)]
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == [("enable", "A")]
        assert "- Gone: not in the account, or changed since the backup" in result.output

    def test_nothing_to_change(self, cli_env):
        BackupManager(cli_env).create_backup([_filter("A", "a@x")], sieve_script=OLD_SCRIPT)
        FakeBrowser.current = [_filter("A", "a@x")]
        FakeBrowser.live_script = OLD_SCRIPT
        result = runner.invoke(app, ["restore", "--backup", "latest"])
        assert result.exit_code == 0, result.output
        assert "Nothing to change" in result.output
        assert FakeBrowser.calls == []


def test_rollback_help_describes_full_rollback():
    text = _rollback_help("2026-01-01_00-00-00", Path("/snaps/2026-01-01_00-00-00"))
    assert "restore --backup 2026-01-01_00-00-00" in text
    assert "Sieve script" in text and "UI" in text
    assert "/snaps/2026-01-01_00-00-00/backup.json" in text
    assert "safety backup" in text
