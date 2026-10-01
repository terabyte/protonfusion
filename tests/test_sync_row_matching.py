"""Sync guards around row identity and the script it uploads (no browser).

Each test here pins one guard that a mutation run showed no other test
held: which rows sync may toggle, how it tells them apart, and when it
must refuse instead of uploading.
"""

import asyncio

from src.main import _scraped_row_names
from src.models.filter_models import ProtonMailFilter
from src.scraper.protonmail_sync import ProtonMailSync
from tests.test_sync_safety import _filter
from tests.test_toggle_row import TogglePage


def _at(f: ProtonMailFilter, priority: int) -> ProtonMailFilter:
    """A copy of `f` scraped at row `priority`."""
    return f.model_copy(update={"priority": priority})


class TestRowListPassedToToggles:

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
