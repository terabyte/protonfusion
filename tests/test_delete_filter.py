"""Unit tests for ProtonMailSync.delete_filter's row selection (no browser).

delete_filter() finds its row by name, so it must only ever delete a single,
disabled row in the Custom filters section. The page is faked with just the
element calls delete_filter makes.
"""

import asyncio

from src.scraper import selectors
from src.scraper.protonmail_sync import ProtonMailSync


class FakeElement:
    """An element that records clicks on the page that owns it."""

    def __init__(self, page, label, children=None, attrs=None, checked=False):
        self.page = page
        self.label = label
        self.children = children or {}
        self.attrs = attrs or {}
        self.checked = checked

    async def query_selector(self, selector):
        return self.children.get(selector)

    async def query_selector_all(self, selector):
        return self.children.get(selector, [])

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def is_checked(self):
        return self.checked

    async def is_visible(self):
        return True

    async def click(self):
        self.page.clicks.append(self.label)


class FakePage:
    """A filters page whose Custom filters section holds `rows` as (name, enabled).

    The same rows are also reachable page-wide, as they are in ProtonMail, so a
    lookup that ignores the section would still find them.
    """

    def __init__(self, rows):
        self.clicks = []
        self.rows = []
        for i, (name, enabled) in enumerate(rows):
            self.rows.append(FakeElement(self, f"row{i}", children={
                selectors.FILTER_EDIT_BUTTON: FakeElement(
                    self, f"edit{i}", attrs={"aria-label": f'Edit filter "{name}"'},
                ),
                selectors.FILTER_TOGGLE: FakeElement(self, f"toggle{i}", checked=enabled),
                selectors.FILTER_ACTIONS_DROPDOWN: FakeElement(self, f"dropdown{i}"),
            }))
        self.section = FakeElement(self, "section", children={selectors.FILTER_TABLE_ROWS: self.rows})
        self.delete_item = FakeElement(self, "delete-item")
        self.confirm = FakeElement(self, "confirm")

    async def query_selector(self, selector):
        if selector == selectors.CUSTOM_FILTERS_SECTION:
            return self.section
        if selector == selectors.DELETE_CONFIRM_BUTTON:
            return self.confirm
        if "Delete" in selector:
            return self.delete_item
        return None

    async def query_selector_all(self, selector):
        return self.rows if selector == selectors.FILTER_TABLE_ROWS else []

    async def wait_for_timeout(self, ms):
        pass


def _delete(rows, name):
    """Run delete_filter(name) against a fake page; return (result, clicks)."""
    sync = ProtonMailSync()
    sync.page = FakePage(rows)
    result = asyncio.run(sync.delete_filter(name))
    return result, sync.page.clicks


def test_deletes_single_disabled_row():
    result, clicks = _delete([("Keep", True), ("Old", False)], "Old")
    assert result is True
    assert clicks == ["dropdown1", "delete-item", "confirm"]


def test_refuses_enabled_row():
    result, clicks = _delete([("Live", True)], "Live")
    assert result is False
    assert clicks == []


def test_refuses_duplicate_name():
    """Two rows named "News": either could be the one meant, so neither goes."""
    result, clicks = _delete([("News", True), ("News", False)], "News")
    assert result is False
    assert clicks == []


def test_refuses_duplicate_disabled_names():
    result, clicks = _delete([("News", False), ("News", False)], "News")
    assert result is False
    assert clicks == []


def test_missing_name_deletes_nothing():
    result, clicks = _delete([("Other", False)], "Gone")
    assert result is False
    assert clicks == []


def test_no_custom_filters_section_deletes_nothing():
    """Rows outside the Custom filters section are not filters this tool manages."""
    sync = ProtonMailSync()
    sync.page = FakePage([("Old", False)])
    sync.page.section = None
    assert asyncio.run(sync.delete_filter("Old")) is False
    assert sync.page.clicks == []
