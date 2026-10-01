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


class SwitchLabel(FakeElement):
    """A row's switch label: clicking it flips the row's toggle, unless the switch is stuck."""

    def __init__(self, page, label, toggle, stuck=False):
        super().__init__(page, label)
        self.toggle = toggle
        self.stuck = stuck

    async def click(self):
        await super().click()
        if not self.stuck:
            self.toggle.checked = not self.toggle.checked


class TogglePage(FakePage):
    """FakePage whose rows also have the toggle switch label sync clicks.

    `stuck` holds the row indexes whose switch ignores clicks, as ProtonMail's
    does when it refuses an enable at the active-filter limit.
    """

    def __init__(self, rows, stuck=()):
        super().__init__(rows)
        for i, row in enumerate(self.rows):
            row.children[selectors.FILTER_TOGGLE_LABEL] = SwitchLabel(
                self, f"switch{i}", row.children[selectors.FILTER_TOGGLE], stuck=i in stuck,
            )


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


def test_click_that_does_not_change_the_switch_is_a_failure():
    """V5: the click is swallowed (a modal, or Proton refusing at the filter limit).

    sync's unchanged-script path takes True as proof ProtonFusion's filter is
    on after the UI filters are off, so a switch still off must read as False.
    """
    sync = ProtonMailSync()
    sync.page = TogglePage([("ProtonFusion Consolidated", False)], stuck={0})
    result = asyncio.run(sync.set_row_enabled(0, "ProtonFusion Consolidated", True))
    assert sync.page.clicks == ["switch0"]
    assert result is False
    assert sync.last_toggle_refused is True


def test_click_that_changes_the_switch_is_confirmed():
    sync = ProtonMailSync()
    sync.page = TogglePage([("A", False)])
    assert asyncio.run(sync.set_row_enabled(0, "A", True)) is True
    assert sync.page.rows[0].children[selectors.FILTER_TOGGLE].checked is True
    assert sync.last_toggle_refused is False


def test_toggle_by_name_reads_the_switch_back():
    """enable_filter / disable_filter share the read-back."""
    sync = ProtonMailSync()
    sync.page = TogglePage([("A", True)], stuck={0})
    assert asyncio.run(sync.disable_filter("A")) is False
    assert sync.last_toggle_refused is True


class LateSwitchPage(TogglePage):
    """A click on row 0's switch takes effect only after `delay` waits, as when
    ProtonMail flips the switch once its API call returns."""

    def __init__(self, rows, delay):
        super().__init__(rows)
        self.delay = delay
        self.pending = None
        self.rows[0].children[selectors.FILTER_TOGGLE_LABEL] = _Callback(self, "switch0", self._clicked)

    def _clicked(self):
        self.pending = self.delay

    async def wait_for_timeout(self, ms):
        if self.pending is not None:
            self.pending -= 1
            if self.pending == 0:
                toggle = self.rows[0].children[selectors.FILTER_TOGGLE]
                toggle.checked = not toggle.checked
                self.pending = None


class ReplacingPage(TogglePage):
    """A click on row 0's switch re-renders the list: row 0 becomes a new node in
    the new state, and the old node (with its old switch) is detached."""

    def __init__(self, rows):
        super().__init__(rows)
        self.rows[0].children[selectors.FILTER_TOGGLE_LABEL] = _Callback(self, "switch0", self._clicked)

    def _clicked(self):
        old = self.rows[0]
        fresh = FakeElement(self, "row0-new", children=dict(old.children))
        fresh.children[selectors.FILTER_TOGGLE] = FakeElement(
            self, "toggle0-new", checked=not old.children[selectors.FILTER_TOGGLE].checked,
        )
        self.rows[0] = fresh


class _Callback(FakeElement):
    """A switch label whose click runs `on_click` instead of flipping a toggle itself."""

    def __init__(self, page, label, on_click):
        super().__init__(page, label)
        self.on_click = on_click

    async def click(self):
        await super().click()
        self.on_click()


def test_switch_that_flips_late_is_confirmed():
    """W8: the read-back polls, so a switch that flips a few reads after the click is
    a success, not a refusal."""
    sync = ProtonMailSync()
    sync.page = LateSwitchPage([("A", True)], delay=6)
    assert asyncio.run(sync.set_row_enabled(0, "A", False)) is True
    assert sync.page.clicks == ["switch0"]
    assert sync.last_toggle_refused is False


def test_switch_that_flips_after_the_window_is_unconfirmed():
    from src.scraper.protonmail_sync import TOGGLE_CONFIRM_POLLS
    sync = ProtonMailSync()
    sync.page = LateSwitchPage([("A", True)], delay=TOGGLE_CONFIRM_POLLS + 1)
    assert asyncio.run(sync.set_row_enabled(0, "A", False)) is False
    assert sync.last_toggle_refused is True


def test_row_node_replaced_on_click_is_found_again():
    """W8: the read-back re-queries the row by position and name rather than reading
    the detached node."""
    sync = ProtonMailSync()
    sync.page = ReplacingPage([("A", False), ("B", True)])
    assert asyncio.run(sync.set_row_enabled(0, "A", True, expected_names=["A", "B"])) is True
    assert sync.last_toggle_refused is False


def test_row_node_replaced_on_click_by_name():
    sync = ProtonMailSync()
    sync.page = ReplacingPage([("A", False)])
    assert asyncio.run(sync.enable_filter("A")) is True
