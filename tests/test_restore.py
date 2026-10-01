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
    """Plan, then apply enables and disables as the restore command does; return (report, sync)."""
    sync = sync or FakeSync()
    engine = RestoreEngine(sync)
    plan = RestoreEngine.plan(Backup(filters=backed_up), current)
    enabled, enable_errors = asyncio.run(engine.apply(plan.to_enable, True))
    disabled, disable_errors = asyncio.run(engine.apply(plan.to_disable, False))
    report = {
        "enabled": enabled,
        "disabled": disabled,
        "not_found": plan.not_found,
        "ambiguous": plan.ambiguous,
        "errors": enable_errors + disable_errors,
        "script_not_restored": plan.script_differs,
    }
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

    @staticmethod
    def _subject(kind, enabled, priority) -> ProtonMailFilter:
        """Filter "N" (Trash) on subject "a, b": one literal value, or the two chips a and b.

        Different filters (different content_hash) that share a legacy_identity."""
        cond = {"type": "subject", "operator": "contains"}
        cond.update({"value": "a, b"} if kind == "literal" else {"values": ["a", "b"]})
        return ProtonMailFilter(
            name="N", enabled=enabled, priority=priority, conditions=[cond],
            actions=[{"type": "trash"}], raw={"conditions_text": "c", "actions_text": "a"},
        )

    def test_exact_content_pairs_before_legacy_identity(self):
        """W2: a literal "a, b" and the chips [a, b] are different filters; after a reorder
        each still pairs with itself, so nothing is toggled."""
        lit, chips = self._subject("literal", True, 0), self._subject("chips", False, 1)
        backup = Backup(version="1.3", filters=[lit, chips])
        live = [self._subject("chips", False, 0), self._subject("literal", True, 1)]
        plan = RestoreEngine.plan(backup, live)
        assert plan.to_enable == [] and plan.to_disable == []
        assert plan.ambiguous == [] and plan.already_correct == ["N", "N"]

    def test_legacy_group_of_different_filters_is_ambiguous(self):
        """W2: in a backup older than 1.3 a literal may be a legacy encoding of chips, so
        exact content proves nothing; a legacy group of differing filters is never paired
        by row order."""
        backup = Backup(version="1.2", filters=[self._subject("literal", True, 0), self._subject("chips", False, 1)])
        live = [self._subject("chips", False, 0), self._subject("literal", True, 1)]
        plan = RestoreEngine.plan(backup, live)
        assert plan.to_enable == [] and plan.to_disable == []
        assert len(plan.ambiguous) == 2 and "cannot tell which is which" in plan.ambiguous[0]

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

    def test_toggle_error_text_is_scrubbed(self, caplog):
        """V7: a Playwright error can carry the page URL, whose fragment holds a session key."""

        class RaisingSync(FakeSync):
            async def set_row_enabled(self, index, name, enabled):
                raise RuntimeError("Timeout navigating to https://account.proton.me/u/0/mail#selector=SECRET")

        with caplog.at_level("ERROR"):
            report, _ = _restore(
                [_filter("A", "a@x", enabled=True)],
                [_filter("A", "a@x", enabled=False, priority=0)],
                sync=RaisingSync(),
            )
        assert report["errors"] == ["A: failed to enable: Timeout navigating to https://account.proton.me/u/0/mail"]
        assert "SECRET" not in caplog.text
        assert "A" in caplog.text

    def test_switch_that_ignores_the_click_is_an_error(self):
        """V5: the real set_row_enabled, over a page whose switch does not change on click."""
        from src.scraper.protonmail_sync import ProtonMailSync
        from tests.test_toggle_row import TogglePage
        sync = ProtonMailSync()
        sync.page = TogglePage([("A", False)], stuck={0})
        report, _ = _restore(
            [_filter("A", "a@x", enabled=True)],
            [_filter("A", "a@x", enabled=False, priority=0)],
            sync=sync,
        )
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
    toggle_refuses: set = set()  # (name, enabled) pairs whose switch ignores the click
    limit = None  # active-filter limit, when set; enables over it are refused
    switch_state: dict = {}  # row -> on, tracked when `limit` is set
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
        self.last_toggle_refused = (name, enabled) in FakeBrowser.toggle_refuses
        if (name, enabled) in FakeBrowser.toggle_fails or self.last_toggle_refused:
            return False
        if FakeBrowser.limit is not None:
            # ProtonMail's active-filter limit: an enable over it does not take
            on = FakeBrowser.switch_state
            if not on:
                on.update({f.priority: f.enabled for f in FakeBrowser.current})
            if enabled and not on[index] and sum(on.values()) >= FakeBrowser.limit:
                self.last_toggle_refused = True
                return False
            on[index] = enabled
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
    FakeBrowser.toggle_refuses = set()
    FakeBrowser.limit = None
    FakeBrowser.switch_state = {}
    FakeBrowser.calls = []
    return snapshots_dir


def _snapshot_dirs(snapshots_dir):
    return sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir() and not p.is_symlink())


@pytest.fixture
def after_sync(cli_env):
    """The account after a sync: the backup (taken before it) had the UI filters
    "Old" and "New" on and ProtonFusion's script OLD_SCRIPT; the sync disabled
    both, added "New" to the script, and someone since switched "Spare" on.
    Restoring switches "New" back on, so its rule is not lost with NEW_SCRIPT."""
    BackupManager(cli_env).create_backup([
        _filter("Old", "old@x", enabled=True, priority=0),
        _filter("Spare", "spare@x", enabled=False, priority=1),
        _filter("New", "new@x", enabled=True, priority=2),
        _sieve(SIEVE_FILTER_NAME, OLD_SCRIPT, enabled=True, priority=3),
    ], sieve_script=OLD_SCRIPT)
    FakeBrowser.current = [
        _filter("Old", "old@x", enabled=False, priority=0),
        _filter("Spare", "spare@x", enabled=True, priority=1),
        _filter("New", "new@x", enabled=False, priority=2),
        _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=3),
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
            ("enable", "New"),
            ("upload", OLD_SCRIPT, SIEVE_FILTER_NAME),
            ("disable", "Spare"),
        ]
        assert "Restore complete." in result.output

    def test_unchanged_script_is_not_uploaded(self, after_sync):
        """Proton keeps Save disabled for an unchanged script; trailing whitespace is no change."""
        FakeBrowser.live_script = OLD_SCRIPT + "  \n\n"
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [("enable", "Old"), ("enable", "New"), ("disable", "Spare")]
        assert "Sieve script: already as in the backup" in result.output

    def test_safety_backup_taken_and_named(self, after_sync):
        latest_before = (after_sync / "latest").resolve()
        dirs_before = _snapshot_dirs(after_sync)
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        [safety_id] = sorted(set(_snapshot_dirs(after_sync)) - set(dirs_before))
        assert f"restore --backup {safety_id}" in result.output
        safety = BackupManager(after_sync).load_backup(safety_id)
        assert safety.sieve_script == NEW_SCRIPT
        assert [f.enabled for f in safety.filters] == [False, True, False, True]
        # It describes the pre-restore account, so it does not become 'latest'
        assert (after_sync / "latest").resolve() == latest_before

    def test_preview_shows_changes_and_diff(self, after_sync):
        result = runner.invoke(app, ["restore", "--backup", "latest", "--dry-run"])
        assert result.exit_code == 0, result.output
        snapshot = BackupManager(after_sync).snapshot_dir_for("latest").name
        assert f"Restore preview: backup '{snapshot}'" in result.output
        assert "Will enable 2 filter(s)" in result.output and "- Old (row 0)" in result.output
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
        assert FakeBrowser.calls == [
            ("enable", "Old"), ("enable", "New"), ("upload", OLD_SCRIPT, SIEVE_FILTER_NAME),
        ]
        assert "Restore did NOT complete." in result.output
        assert "not confirmed: the upload did not complete" in result.output
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
        assert FakeBrowser.calls == [("enable", "New")]
        assert "Could not enable" in result.output and "- Old" in result.output

    def test_backup_without_script_refuses_without_flag(self, cli_env):
        BackupManager(cli_env).create_backup(
            [_filter("Old", "old@x", enabled=True), _filter("New", "new@x", enabled=True, priority=1)],
            sieve_script="",
        )
        FakeBrowser.current = [
            _filter("Old", "old@x", enabled=False, priority=0),
            _filter("New", "new@x", enabled=False, priority=1),
            _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=2),
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
        # ProtonFusion's filter goes off first: the active-filter limit counts it (V8)
        assert FakeBrowser.calls == [("disable", SIEVE_FILTER_NAME), ("enable", "Old"), ("enable", "New")]
        assert f"'{SIEVE_FILTER_NAME}' disabled" in result.output

    def test_backup_predating_protonfusion_section(self, cli_env):
        """A backed-up script with no ProtonFusion section: restoring it removes the section.

        No backed-up filter carries the section's rules, so a real restore would refuse."""
        user_script = 'require ["fileinto"];\nif header :contains "subject" "x" { fileinto "X"; }\n'
        BackupManager(cli_env).create_backup(
            [_sieve(SIEVE_FILTER_NAME, user_script)], sieve_script=user_script,
        )
        FakeBrowser.current = [_sieve(SIEVE_FILTER_NAME, NEW_SCRIPT)]
        FakeBrowser.live_script = NEW_SCRIPT
        result = runner.invoke(app, ["restore", "--backup", "latest", "--dry-run"])
        assert result.exit_code == 1, result.output
        assert "REMOVES ProtonFusion's" in result.output
        assert "section (2 condition/action pairs)" in result.output
        assert "A real restore would REFUSE" in result.output

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
    text = _rollback_help(Path("/snaps/2026-01-01_00-00-00"))
    assert "restore --backup 2026-01-01_00-00-00" in text
    assert "Sieve script" in text and "UI" in text
    assert "/snaps/2026-01-01_00-00-00/backup.json" in text
    assert "safety backup" in text


class TestRestoreLeavesNoRuleInNeitherPlace:
    """V2: the backed-up script can lack rules whose filters restore cannot switch back on.

    After sync (which disabled "New" and added its rule to the script) and
    cleanup (which deleted "New") or an edit to "New", reverting the script
    would leave new@x's mail filtered by nothing. Restore must refuse.
    """

    @pytest.fixture
    def synced(self, cli_env):
        """Backup: "Old" and "New" on, script holding only Old's rule. Live: NEW_SCRIPT."""
        old = _filter("Old", "old@x", priority=0)
        new = _filter("New", "new@x", priority=1)
        BackupManager(cli_env).create_backup(
            [old, new, _sieve(SIEVE_FILTER_NAME, OLD_SCRIPT, priority=2)], sieve_script=OLD_SCRIPT,
        )
        FakeBrowser.live_script = NEW_SCRIPT
        return old

    def test_refuses_after_cleanup_deleted_the_filter(self, synced):
        FakeBrowser.current = [synced, _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, priority=1)]
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == []
        assert "Restore refused. Nothing was changed." in result.output
        assert 'address from :is "new@x"' in result.output
        assert "--allow-rule-removal" in result.output

    def test_refuses_when_the_filter_was_edited_since(self, synced):
        edited = _filter("New", "new@x", enabled=False, priority=1, action=ActionType.STAR)
        FakeBrowser.current = [synced, edited, _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, priority=2)]
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == []

    def test_dry_run_says_a_real_restore_would_refuse(self, synced):
        FakeBrowser.current = [synced, _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, priority=1)]
        result = runner.invoke(app, ["restore", "--backup", "latest", "--dry-run"])
        assert result.exit_code == 1, result.output
        assert "A real restore would REFUSE" in result.output

    def test_allow_rule_removal_proceeds(self, synced):
        FakeBrowser.current = [synced, _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, priority=1)]
        result = runner.invoke(
            app, ["restore", "--backup", "latest", "--allow-rule-removal"], input="y\n",
        )
        # Exit 1 still: "New" is reported as a filter it could not restore
        assert result.exit_code == 1, result.output
        assert "--allow-rule-removal given: proceeding anyway." in result.output
        assert FakeBrowser.calls == [("upload", OLD_SCRIPT, SIEVE_FILTER_NAME)]

    def test_filter_switched_back_on_covers_its_rule(self, synced):
        """The sync's disabled "New" is still there, so restore re-enables it: no refusal."""
        FakeBrowser.current = [
            synced, _filter("New", "new@x", enabled=False, priority=1),
            _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, priority=2),
        ]
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [("enable", "New"), ("upload", OLD_SCRIPT, SIEVE_FILTER_NAME)]

    def test_switching_protonfusion_off_counts_as_dropping_its_rules(self, cli_env):
        """Script unchanged, but the backup has ProtonFusion's filter off and no UI filter for new@x."""
        old = _filter("Old", "old@x", priority=0)
        BackupManager(cli_env).create_backup(
            [old, _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=False, priority=1)], sieve_script=NEW_SCRIPT,
        )
        FakeBrowser.current = [old, _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=1)]
        FakeBrowser.live_script = NEW_SCRIPT
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == []
        assert 'address from :is "new@x"' in result.output
        assert 'address from :is "old@x"' not in result.output


    def test_legacy_form_in_the_backed_up_script_carries_the_live_rule(self, cli_env):
        """W4: the backup's script was written by the old generator (`discard;` for Trash),
        the live one has the corrected `fileinto "trash";`. Same rule, so no refusal."""
        t = _filter("T", "t@x", enabled=False, priority=0, action=ActionType.TRASH)
        live = _section_for([t])
        assert 'fileinto "trash";' in live
        legacy = live.replace('fileinto "trash";', "discard;")
        BackupManager(cli_env).create_backup([t, _sieve(SIEVE_FILTER_NAME, legacy, priority=1)], sieve_script=legacy)
        FakeBrowser.current = [t, _sieve(SIEVE_FILTER_NAME, live, priority=1)]
        FakeBrowser.live_script = live
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "filtered by nothing" not in result.output
        assert FakeBrowser.calls == [("upload", legacy, SIEVE_FILTER_NAME)]

    def test_legacy_form_still_needs_the_same_rule(self):
        """W4: accepting legacy forms does not accept a different rule."""
        from src.backup.restore_engine import uncovered_live_rules
        t = _filter("T", "t@x", action=ActionType.TRASH)
        live = _section_for([t])
        other = _section_for([_filter("T", "other@x", action=ActionType.TRASH)]).replace('fileinto "trash";', "discard;")
        assert len(uncovered_live_rules(live, other, [])) == 1
        assert uncovered_live_rules(live, live.replace('fileinto "trash";', "discard;"), []) == []


class TestRestoreFilterLimit:
    """V8: ProtonMail limits active filters, and ProtonFusion's own filter counts."""

    @pytest.fixture
    def pf_off_in_backup(self, cli_env):
        """Backup: "Old" and "New" on, ProtonFusion's filter off. Live: the reverse, same script."""
        BackupManager(cli_env).create_backup([
            _filter("Old", "old@x", priority=0),
            _filter("New", "new@x", priority=1),
            _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=False, priority=2),
        ], sieve_script=NEW_SCRIPT)
        FakeBrowser.current = [
            _filter("Old", "old@x", enabled=False, priority=0),
            _filter("New", "new@x", enabled=False, priority=1),
            _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=2),
        ]
        FakeBrowser.live_script = NEW_SCRIPT
        return cli_env

    def test_protonfusion_filter_switched_off_before_enabling(self, pf_off_in_backup):
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [
            ("disable", SIEVE_FILTER_NAME), ("enable", "Old"), ("enable", "New"),
        ]

    def test_failed_enable_switches_protonfusion_back_on(self, pf_off_in_backup):
        """Keeps the guarantee: a failure part-way leaves every rule that was active active."""
        FakeBrowser.toggle_fails = {("New", True)}
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == [
            ("disable", SIEVE_FILTER_NAME), ("enable", "Old"), ("disable", "Old"), ("enable", SIEVE_FILTER_NAME),
        ]
        assert "no mail is left unfiltered" in result.output
        assert "was switched back on" in result.output.replace("\n", "")

    def test_protonfusion_not_back_on_is_reported(self, pf_off_in_backup):
        FakeBrowser.toggle_fails = {("New", True), (SIEVE_FILTER_NAME, True)}
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1, result.output
        assert "could not be switched back on" in result.output
        assert "no mail is left unfiltered" not in result.output

    def test_upload_with_protonfusion_off_in_backup(self, cli_env):
        """Off first, then enable, upload (which switches it on), then off again."""
        BackupManager(cli_env).create_backup([
            _filter("Old", "old@x", priority=0),
            _filter("New", "new@x", priority=1),
            _sieve(SIEVE_FILTER_NAME, OLD_SCRIPT, enabled=False, priority=2),
        ], sieve_script=OLD_SCRIPT)
        FakeBrowser.current = [
            _filter("Old", "old@x", enabled=False, priority=0),
            _filter("New", "new@x", enabled=False, priority=1),
            _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=2),
        ]
        FakeBrowser.live_script = NEW_SCRIPT
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 0, result.output
        assert FakeBrowser.calls == [
            ("disable", SIEVE_FILTER_NAME), ("enable", "Old"), ("enable", "New"),
            ("upload", OLD_SCRIPT, SIEVE_FILTER_NAME), ("disable", SIEVE_FILTER_NAME),
        ]
        assert "Disabled: 1 of 1" in result.output

    def test_refused_enable_reported_as_probable_limit(self, after_sync):
        """ProtonFusion's filter stays on in the backup, so the order is unchanged; say why it stopped."""
        FakeBrowser.toggle_refuses = {("New", True)}
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == [("enable", "Old")]
        assert "active-filter limit" in result.output
        assert "Enabled: 1 of 2 (Old)" in result.output

    def test_protonfusion_not_found_is_not_reported_as_switched_off(self, pf_off_in_backup):
        """W7: its row cannot be identified, so nothing is clicked and nothing went off;
        the report must not say it was switched off and could not come back on."""
        FakeBrowser.toggle_fails = {(SIEVE_FILTER_NAME, False), (SIEVE_FILTER_NAME, True)}
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        flat = " ".join(result.output.split())
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == []
        assert "was switched off first" not in flat
        assert "could not be switched back on" not in flat
        assert "no mail is left unfiltered" in flat

    @pytest.fixture
    def two_on_in_backup(self, cli_env):
        """Backup: ProtonFusion's filter off, A and B on. Now: it is on (its script holds A
        and B), A and B are off."""
        a = _filter("A", "a@x", enabled=True, priority=0)
        b = _filter("B", "b@x", enabled=True, priority=1)
        script = _section_for([a, b])
        BackupManager(cli_env).create_backup(
            [a, b, _sieve(SIEVE_FILTER_NAME, script, enabled=False, priority=2)], sieve_script=script,
        )
        FakeBrowser.current = [
            _filter("A", "a@x", enabled=False, priority=0), _filter("B", "b@x", enabled=False, priority=1),
            _sieve(SIEVE_FILTER_NAME, script, enabled=True, priority=2),
        ]
        FakeBrowser.live_script = script
        return cli_env

    def test_at_the_limit_enables_are_undone_before_protonfusion_goes_back_on(self, two_on_in_backup):
        """W1: one active filter allowed. A takes the slot ProtonFusion's filter freed, B is
        refused; A must go off again so ProtonFusion's filter can come back on, or B's rule
        would be in no running filter."""
        FakeBrowser.limit = 1
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        flat = " ".join(result.output.split())
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == [
            ("disable", SIEVE_FILTER_NAME), ("enable", "A"), ("disable", "A"), ("enable", SIEVE_FILTER_NAME),
        ]
        assert "was switched back on" in flat
        assert "could not be switched back on" not in flat
        assert "Switched back off to free their active-filter slots" in flat

    def test_protonfusion_still_off_lists_the_rules_filtered_by_nothing(self, two_on_in_backup):
        """W1: it cannot come back on even with A off, so A goes back on (it carries A's rule)
        and the report names exactly the rule now in no running filter: B's."""
        FakeBrowser.toggle_refuses = {("B", True), (SIEVE_FILTER_NAME, True)}
        result = runner.invoke(app, ["restore", "--backup", "latest"], input="y\n")
        flat = " ".join(result.output.split())
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == [
            ("disable", SIEVE_FILTER_NAME), ("enable", "A"), ("disable", "A"), ("enable", "A"),
        ]
        assert "could not be switched back on" in flat
        assert "1 rule(s) of its section are in no running filter" in flat
        assert 'address from :is "b@x"' in flat
        assert 'address from :is "a@x"' not in flat
