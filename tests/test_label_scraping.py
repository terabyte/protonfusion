"""Integration tests for reading "Label as" actions from the mock filters page.

These launch a real Chromium browser via Playwright against the synthetic
fixture in tests/fixtures/mock_filters_page.html, whose Actions step copies
the label row captured from the live UI on 2026-09-30: every account label
is a checkbox (with a label chip) and the applied ones are ticked through
the `checked` property only.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

from src.scraper.protonmail_scraper import ProtonMailScraper

MOCK_HTML = Path(__file__).parent / "fixtures" / "mock_filters_page.html"
MOCK_URL = f"file://{MOCK_HTML.resolve()}"


async def _scrape_mock(url: str = MOCK_URL, workers: int = 1) -> dict:
    """Scrape the mock page and return filters keyed by name."""
    scraper = ProtonMailScraper(headless=True)
    try:
        await scraper.initialize()
        await scraper.page.goto(url, wait_until="domcontentloaded")
        await scraper.page.wait_for_timeout(500)
        filters = await scraper.scrape_all_filters(workers=workers)
        return {f["name"]: f for f in filters}
    finally:
        await scraper.close()


def _labels(filter_data: dict) -> list:
    return [a["parameters"]["label"] for a in filter_data["actions"] if a["type"] == "label"]


@pytest.mark.asyncio
async def test_only_ticked_labels_read():
    """Only ticked label checkboxes count; the unticked account labels do not."""
    filters = await _scrape_mock()

    assert _labels(filters["Work Emails"]) == ["Work"]
    assert _labels(filters["Finance Reports"]) == ["Finance", "Taxes"]
    assert _labels(filters["VIP Senders"]) == []  # every label listed, none ticked

    # The first filter with a folder is where the folder map gets built
    # (which presses Escape and closes the wizard); its label and the
    # rest of its Actions step must still be read.
    newsletter = filters["Newsletter Trash"]
    assert _labels(newsletter) == ["pf-test-label"]
    assert any(a["type"] == "delete" for a in newsletter["actions"])
    assert newsletter["scrape_issues"] == []

    # A filter that moves, labels twice, marks read and stars keeps every action.
    multi = filters["Multi-tag Filter"]
    types = [a["type"] for a in multi["actions"]]
    assert _labels(multi) == ["Alerts", "On Call"]
    assert "move_to" in types
    assert "mark_read" in types
    assert "star" in types


@pytest.mark.asyncio
async def test_default_filters_complete_with_evidence():
    """Every filter on the default mock page reads cleanly and keeps raw text."""
    filters = await _scrape_mock()

    for name, f in filters.items():
        assert f["scrape_issues"] == [], name
        assert "Conditions" in f["raw"]["conditions_text"], name
        assert "Actions" in f["raw"]["actions_text"], name

    # The raw text holds what the parser reads, and form state innerText omits.
    finance = filters["Finance Reports"]["raw"]
    assert "finance@company.com" in finance["conditions_text"]
    assert "[checkbox] Finance: checked" in finance["actions_text"]
    assert "[checkbox] Work: unchecked" in finance["actions_text"]
    assert "[checkbox] Starred: checked" in finance["actions_text"]
