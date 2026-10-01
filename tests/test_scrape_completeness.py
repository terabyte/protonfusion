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


def test_unknown_action_row_flagged(edge_filters):
    issues = edge_filters["Has Autoreply"]["scrape_issues"]
    assert any("filter-modal:autoreply-row" in i for i in issues)
    # The raw evidence still records what the unknown row showed.
    assert "Send auto-reply" in edge_filters["Has Autoreply"]["raw"]["actions_text"]


def test_unrecognised_label_layout_flagged(edge_filters):
    issues = edge_filters["Plain Label Text"]["scrape_issues"]
    assert any("Receipts" in i for i in issues)


def test_label_count_flagged(edge_filters):
    issues = edge_filters["Label Count Only"]["scrape_issues"]
    assert any("count" in i for i in issues)


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
