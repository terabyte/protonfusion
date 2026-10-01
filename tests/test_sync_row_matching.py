"""Sync guards around row identity and the script it uploads (no browser).

Each test here pins one guard that a mutation run showed no other test
held: which rows sync may toggle, how it tells them apart, and when it
must refuse instead of uploading.
"""

import asyncio

import pytest
from typer.testing import CliRunner

from src.backup.backup_manager import BackupManager
from src.backup.sync_plan import plan_disable
from src.consolidator.carry_forward import filter_facts
from src.main import SIEVE_FILTER_NAME, _scraped_row_names, app
from src.models.filter_models import ProtonMailFilter, ScrapeEvidence
from src.scraper.protonmail_sync import ProtonMailSync
from tests.test_sync_safety import (  # noqa: F401 (fixtures)
    FakeScraper, FakeSync, _filter, _wide_console, cli_snapshots_dir, fake_sync,
)
from tests.test_toggle_row import TogglePage

runner = CliRunner()


def _pf_row(enabled: bool = True, priority: int = 0) -> ProtonMailFilter:
    """ProtonFusion's own Sieve filter as the scrape reads it."""
    return ProtonMailFilter(
        name=SIEVE_FILTER_NAME, enabled=enabled, priority=priority, is_sieve=True,
        raw=ScrapeEvidence(sieve_text="keep;"),
    )


def _at(f: ProtonMailFilter, priority: int) -> ProtonMailFilter:
    """A copy of `f` scraped at row `priority`."""
    return f.model_copy(update={"priority": priority})


@pytest.fixture
def toggle_log(monkeypatch):
    """Record the expected_names every set_row_enabled call receives, in call order."""
    log = []
    original = FakeSync.set_row_enabled

    async def recording(self, index, name, enabled, expected_names=None, require_current=None):
        log.append((name, enabled, expected_names))
        return await original(
            self, index, name, enabled, expected_names=expected_names, require_current=require_current,
        )

    monkeypatch.setattr(FakeSync, "set_row_enabled", recording)
    return log


@pytest.fixture
def consolidated(cli_snapshots_dir, fake_sync):
    """Two wizard filters backed up and consolidated; returns them."""
    filters = [_filter("a@x.com"), _filter("b@x.com")]
    BackupManager(cli_snapshots_dir).create_backup(filters)
    assert runner.invoke(app, ["consolidate"]).exit_code == 0
    return filters


class TestRowListPassedToToggles:

    def test_row_list_passed_to_every_toggle(self, consolidated, fake_sync, toggle_log):
        """Disables and the re-enables after a failed upload all check the scraped list (SY15)."""
        a, b = consolidated
        FakeScraper.filters = [_at(a, 0), _at(b, 1), _pf_row(priority=2)]
        fake_sync.upload_result = False

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        scraped = [a.name, b.name, SIEVE_FILTER_NAME]
        assert [(n, e) for n, e, _ in toggle_log] == [
            (a.name, False), (b.name, False), (a.name, True), (b.name, True),
        ]
        assert all(names == scraped for _, _, names in toggle_log)

    def test_scrape_with_gap_never_trusts_position(self):
        """Row 1 was not scraped, so the list cannot vouch for any position (SY16).

        The live list now has two rows named "A". The one scraped at row 0
        may have been deleted and the unscraped one moved up into its place,
        so a toggle by position could hit a filter sync never matched.
        """
        scraped = [_at(_filter("a@x.com"), 0), _at(_filter("c@x.com"), 2)]
        scraped = [f.model_copy(update={"name": "A"}) for f in scraped]
        expected = _scraped_row_names(scraped)

        sync = ProtonMailSync()
        sync.page = TogglePage([("A", True), ("A", True)])
        result = asyncio.run(sync.set_row_enabled(0, "A", False, expected_names=expected))
        assert result is False
        assert sync.page.clicks == []

    def test_scrape_with_gap_still_follows_a_unique_name(self):
        scraped = [_at(_filter("a@x.com"), 0), _at(_filter("c@x.com"), 2)]
        expected = _scraped_row_names(scraped)

        sync = ProtonMailSync()
        sync.page = TogglePage([("Other", True), (scraped[1].name, True)])
        result = asyncio.run(sync.set_row_enabled(2, scraped[1].name, False, expected_names=expected))
        assert result is True
        assert sync.page.clicks == ["switch1"]


class TestUnchangedScriptRowChecks:
    """The "already up to date" path must find exactly one ProtonFusion Sieve filter."""

    @pytest.fixture
    def unchanged(self, cli_snapshots_dir, fake_sync):
        """A backed-up filter whose consolidated script is already live; returns the filter."""
        from src.generator.sieve_generator import SieveGenerator
        f = _filter("a@x.com")
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([f])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        generated = (manager.snapshot_dir_for("latest") / "consolidated.sieve").read_text()
        fake_sync.live_script = SieveGenerator.merge_with_existing(generated, "")
        return f

    def test_two_script_filters_named_alike_refuses(self, unchanged, fake_sync):
        """Two Sieve filters named like ProtonFusion's: no telling which holds the script (UC2)."""
        FakeScraper.filters = [_at(unchanged, 0), _pf_row(priority=1), _pf_row(enabled=False, priority=2)]
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1, result.output
        assert "2 Sieve filters named" in result.output
        assert "Sync refused" in result.output
        assert fake_sync.calls == []

    def test_unchanged_path_ignores_wizard_filter_named_like_pf(self, unchanged, fake_sync):
        """A wizard filter sharing the name is not a second script holder (UC4)."""
        lookalike = _filter("other@x.com").model_copy(update={"name": SIEVE_FILTER_NAME, "priority": 2})
        FakeScraper.filters = [_at(unchanged, 0), _pf_row(priority=1), lookalike]
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert fake_sync.calls == [("disable", unchanged.name)]
        assert "Sieve uploaded: No (already up to date)" in result.output


def test_valid_merge_failing_validation_refuses(cli_snapshots_dir, fake_sync):
    """A merge that succeeds can still be invalid Sieve; sync refuses it (SY7).

    The user's rule uses addflag without requiring imap4flags. It parses,
    so the merge goes through, but the merged script is not valid.
    """
    user_script = 'if true { addflag "x"; }\n'
    f = _filter("a@x.com")
    BackupManager(cli_snapshots_dir).create_backup([f], sieve_script=user_script)
    assert runner.invoke(app, ["consolidate"]).exit_code == 0
    fake_sync.live_script = user_script
    FakeScraper.filters = [_at(f, 0), _pf_row(priority=1)]

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1, result.output
    assert "does not parse" in result.output
    assert "Sync refused" in result.output
    assert fake_sync.calls == []


def test_row_named_like_pf_but_not_flagged_sieve_never_disabled():
    """A wizard row named like ProtonFusion's filter is left alone even if covered (S2)."""
    lookalike = _filter("a@x.com").model_copy(update={"name": SIEVE_FILTER_NAME})
    plan = plan_disable(
        [lookalike], {lookalike.content_hash}, [lookalike], SIEVE_FILTER_NAME, filter_facts(lookalike),
    )
    assert plan.to_disable == []
    assert plan.sieve == [lookalike]


class TestFilterIsEnabled:
    """_filter_is_enabled reads the toggle of the one row with the name (UP7)."""

    @staticmethod
    def _is_enabled(rows, name):
        sync = ProtonMailSync()
        sync.page = TogglePage(rows)
        return asyncio.run(sync._filter_is_enabled(name))

    def test_filter_is_enabled_reads_the_toggle_of_the_unique_row(self):
        assert self._is_enabled([("Other", True), (SIEVE_FILTER_NAME, False)], SIEVE_FILTER_NAME) is False
        assert self._is_enabled([("Other", False), (SIEVE_FILTER_NAME, True)], SIEVE_FILTER_NAME) is True

    def test_shared_name_is_not_enabled(self):
        rows = [(SIEVE_FILTER_NAME, True), (SIEVE_FILTER_NAME, True)]
        assert self._is_enabled(rows, SIEVE_FILTER_NAME) is False
