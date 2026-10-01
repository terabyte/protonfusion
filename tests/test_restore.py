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
            _filter("News", "drop@x", enabled=False, priority=1, action=ActionType.DELETE),
        ]
        # Since the backup, a sync disabled the first and someone enabled the second
        current = [
            _filter("News", "keep@x", enabled=False, priority=0),
            _filter("News", "drop@x", enabled=True, priority=1, action=ActionType.DELETE),
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


class FakeBrowser:
    """Stands in for both ProtonMailScraper and ProtonMailSync in the CLI test."""
    current: list = []
    calls: list = []

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

    async def set_row_enabled(self, index, name, enabled):
        FakeBrowser.calls.append((index, name, enabled))
        return True

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
    FakeBrowser.calls = []
    return snapshots_dir


class TestRestoreCommand:

    def test_reports_what_it_could_not_restore_and_exits_1(self, cli_env):
        BackupManager(cli_env).create_backup(
            [_filter("A", "a@x", enabled=True), _filter("Gone", "g@x", enabled=True, priority=1)],
            sieve_script="keep;",
        )
        FakeBrowser.current = [_filter("A", "a@x", enabled=False)]
        result = runner.invoke(app, ["restore", "--backup", "latest"])
        assert result.exit_code == 1, result.output
        assert FakeBrowser.calls == [(0, "A", True)]
        assert "Restore incomplete: 1" in result.output
        assert "- Gone: not in the account, or changed since the backup" in result.output
        assert "the Sieve script was not changed" in result.output
        assert '"sieve_script"' in result.output

    def test_complete_restore_exits_0(self, cli_env):
        BackupManager(cli_env).create_backup([_filter("A", "a@x", enabled=True)])
        FakeBrowser.current = [_filter("A", "a@x", enabled=False)]
        result = runner.invoke(app, ["restore", "--backup", "latest"])
        assert result.exit_code == 0, result.output
        assert "Restore complete!" in result.output


def test_rollback_help_says_restore_does_not_change_the_script():
    text = _rollback_help("2026-01-01_00-00-00", Path("/snaps/2026-01-01_00-00-00"))
    assert "restore --backup 2026-01-01_00-00-00" in text
    assert "does NOT change the Sieve script" in text
    assert '"sieve_script"' in text and "/snaps/2026-01-01_00-00-00/backup.json" in text
    assert SIEVE_FILTER_NAME in text
