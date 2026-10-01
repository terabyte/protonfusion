"""Unit tests for ProtonMailSync.set_row_enabled's row selection (no browser).

sync disables, and after a failed upload re-enables, rows it matched by
content during the scrape. It identifies each by its scraped position and
name, so it must toggle that row, follow a row that moved only when its
name is unique, and otherwise click nothing.
"""

import asyncio

from src.scraper import selectors
from src.scraper.protonmail_sync import ProtonMailSync
from tests.test_delete_filter import FakeElement, FakePage


class TogglePage(FakePage):
    """FakePage whose rows also have the toggle switch label sync clicks."""

    def __init__(self, rows):
        super().__init__(rows)
        for i, row in enumerate(self.rows):
            row.children[selectors.FILTER_TOGGLE_LABEL] = FakeElement(self, f"switch{i}")


def _toggle(rows, index, name, enabled):
    """Run set_row_enabled against a fake page; return (result, clicks)."""
    sync = ProtonMailSync()
    sync.page = TogglePage(rows)
    result = asyncio.run(sync.set_row_enabled(index, name, enabled))
    return result, sync.page.clicks


def test_toggles_the_row_at_its_scraped_position():
    """Two rows share a name; the position says which one was matched."""
    result, clicks = _toggle([("News", True), ("News", True)], 1, "News", False)
    assert result is True
    assert clicks == ["switch1"]


def test_row_already_in_state_is_not_clicked():
    result, clicks = _toggle([("A", False)], 0, "A", False)
    assert result is True
    assert clicks == []


def test_moved_row_with_unique_name_is_followed():
    result, clicks = _toggle([("ProtonFusion Consolidated", True), ("A", False)], 0, "A", True)
    assert result is True
    assert clicks == ["switch1"]


def test_moved_row_with_shared_name_is_refused():
    result, clicks = _toggle([("Other", True), ("A", True), ("A", True)], 0, "A", False)
    assert result is False
    assert clicks == []


def test_missing_row_is_refused():
    result, clicks = _toggle([("Other", True)], 3, "Gone", False)
    assert result is False
    assert clicks == []


def _by_name(rows, method, *args):
    """Run a name-keyed ProtonMailSync method against a fake page; return (result, clicks)."""
    sync = ProtonMailSync()
    sync.page = TogglePage(rows)
    result = asyncio.run(getattr(sync, method)(*args))
    return result, sync.page.clicks


def test_toggle_by_name_matches_exactly_not_by_substring():
    result, clicks = _by_name([("A (old copy)", False), ("A", False)], "enable_filter", "A")
    assert result is True
    assert clicks == ["switch1"]


def test_toggle_by_shared_name_is_refused():
    result, clicks = _by_name([("A", False), ("A", False)], "enable_filter", "A")
    assert result is False
    assert clicks == []


def test_ensure_enabled_matches_exactly_not_by_substring():
    _, clicks = _by_name(
        [("ProtonFusion Consolidated (old copy)", False), ("ProtonFusion Consolidated", False)],
        "_ensure_filter_enabled", "ProtonFusion Consolidated",
    )
    assert clicks == ["switch1"]


def test_ensure_enabled_with_shared_name_touches_nothing():
    _, clicks = _by_name(
        [("ProtonFusion Consolidated", False), ("ProtonFusion Consolidated", False)],
        "_ensure_filter_enabled", "ProtonFusion Consolidated",
    )
    assert clicks == []


def _toggle_with(rows, index, name, enabled, **kwargs):
    """set_row_enabled with keyword options; return (result, clicks)."""
    sync = ProtonMailSync()
    sync.page = TogglePage(rows)
    result = asyncio.run(sync.set_row_enabled(index, name, enabled, **kwargs))
    return result, sync.page.clicks


def test_changed_list_does_not_trust_a_shared_name_at_the_position():
    """A row was inserted above: position 1 now holds the other "News"."""
    scraped = ["News", "News", "Other"]
    live = [("Inserted", True), ("News", True), ("News", True), ("Other", True)]
    result, clicks = _toggle_with(live, 1, "News", False, expected_names=scraped)
    assert result is False
    assert clicks == []


def test_unchanged_list_trusts_the_position():
    rows = [("News", True), ("News", True)]
    result, clicks = _toggle_with(rows, 1, "News", False, expected_names=["News", "News"])
    assert result is True
    assert clicks == ["switch1"]


def test_changed_list_still_follows_a_unique_name():
    result, clicks = _toggle_with(
        [("Inserted", True), ("A", True)], 0, "A", False, expected_names=["A"],
    )
    assert result is True
    assert clicks == ["switch1"]


def test_row_not_in_required_state_is_refused():
    """Scraped enabled, but the user switched it off since: do not report it as disabled."""
    result, clicks = _toggle_with([("A", False)], 0, "A", False, require_current=True)
    assert result is False
    assert clicks == []
