"""Integration tests for the scraper's completeness guard.

Scrapes the mock page's edge-case set (?set=edge), whose filters each use
a field or layout the scraper cannot fully read. Each must come back with
scrape_issues naming the problem, never silently reduced to the parts the
scraper did understand.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

from src.scraper.protonmail_scraper import ProtonMailScraper

MOCK_HTML = Path(__file__).parent / "fixtures" / "mock_filters_page.html"
EDGE_URL = f"file://{MOCK_HTML.resolve()}?set=edge"


@pytest.fixture(scope="module")
def edge_filters():
    """Scrape the edge-case set once for all tests in this module."""
    import asyncio

    async def _scrape():
        scraper = ProtonMailScraper(headless=True)
        try:
            await scraper.initialize()
            await scraper.page.goto(EDGE_URL, wait_until="domcontentloaded")
            await scraper.page.wait_for_timeout(500)
            return await scraper.scrape_all_filters(workers=1)
        finally:
            await scraper.close()

    filters = asyncio.run(_scrape())
    return {f["name"]: f for f in filters}


def test_clean_filter_not_flagged(edge_filters):
    f = edge_filters["Clean Labelled"]
    assert f["scrape_issues"] == []
    assert {"type": "label", "parameters": {"label": "Work"}} in f["actions"]


def test_auto_reply_on_flagged(edge_filters):
    issues = edge_filters["Has Autoreply"]["scrape_issues"]
    assert "auto-reply action not supported" in issues


def test_auto_reply_off_not_flagged(edge_filters):
    """The auto-reply row is on every filter; off is the normal case."""
    assert edge_filters["Clean Labelled"]["scrape_issues"] == []


def test_unknown_action_row_flagged(edge_filters):
    f = edge_filters["Unknown Row"]
    assert any("filter-modal:forward-row" in i for i in f["scrape_issues"])
    # The raw evidence still records what the unknown row showed.
    assert "Forward to" in f["raw"]["actions_text"]


def test_unnamed_label_option_flagged(edge_filters):
    issues = edge_filters["Unnamed Label Option"]["scrape_issues"]
    assert any("no readable name" in i for i in issues)


def test_unexplained_label_text_flagged(edge_filters):
    issues = edge_filters["Unexplained Label Text"]["scrape_issues"]
    assert any("Applied: Receipts" in i for i in issues)


def test_unknown_operator_flagged(edge_filters):
    issues = edge_filters["Unknown Operator"]["scrape_issues"]
    assert any("does not contain" in i for i in issues)


def test_unknown_mark_as_option_flagged(edge_filters):
    f = edge_filters["Extra Mark Option"]
    assert any("Pinned" in i for i in f["scrape_issues"])
    assert {"type": "mark_read", "parameters": {}} in f["actions"]


def test_sieve_filter_keeps_script(edge_filters):
    f = edge_filters["My Sieve"]
    assert f["scrape_issues"] == []
    assert 'fileinto "X"' in f["raw"]["sieve_text"]
