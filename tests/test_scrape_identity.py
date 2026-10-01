"""How scraped names and values become filter identity (no browser).

Each test pins one guard that a mutation run showed no other test held.
"""

import asyncio

from src.models.filter_models import (
    ActionType, ConditionType, FilterAction, FilterCondition, Operator, ProtonMailFilter,
)
from src.scraper import selectors
from src.scraper.protonmail_scraper import ProtonMailScraper


def _with_condition(condition: FilterCondition) -> ProtonMailFilter:
    return ProtonMailFilter(
        name="F", conditions=[condition],
        actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Spam"})],
    )


def test_values_list_and_pipe_literal_hash_differently():
    """Two chips "a" and "b" are not one value "a|b" (M3)."""
    chips = _with_condition(FilterCondition(
        type=ConditionType.SUBJECT, operator=Operator.CONTAINS, values=["a", "b"],
    ))
    literal = _with_condition(FilterCondition(
        type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="a|b",
    ))
    assert chips.content_hash != literal.content_hash


class _Row:
    """An element with no children: every lookup inside it finds nothing."""

    async def query_selector(self, selector):
        return None

    async def query_selector_all(self, selector):
        return []


class _ActionsPage:
    """The Actions step with only the "Label as" row present."""

    async def query_selector(self, selector):
        return _Row() if selector == selectors.FILTER_ACTION_LABEL_ROW else None

    async def query_selector_all(self, selector):
        return []


def test_label_with_slash_is_escaped():
    """A "/" in a label name would read as a separator in Sieve (SC2)."""
    scraper = ProtonMailScraper.__new__(ProtonMailScraper)
    scraper._folder_path_map = None

    async def read_label_row(row):
        return ["Clients/Acme"], None

    scraper._read_label_row = read_label_row
    actions, _ = asyncio.run(scraper._scrape_actions(page=_ActionsPage()))
    assert actions == [{"type": "label", "parameters": {"label": "Clients\\/Acme"}}]


def test_system_folder_wins_over_map():
    """A system folder resolves to itself even if the map holds a same-named folder (SC4).

    _scrape_actions turns the plain system name into its fixed action
    (Spam is `fileinto "spam"`); a map path would make it a user folder.
    """
    scraper = ProtonMailScraper.__new__(ProtonMailScraper)
    scraper._folder_path_map = {"Spam": "Work/Spam", "Trash": "Old/Trash"}
    assert scraper._resolve_folder_path("Spam") == "Spam"
    assert scraper._resolve_folder_path("Trash") == "Trash"
