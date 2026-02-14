"""Integration tests for parallel filter scraping using mock HTML page.

These tests launch a real Chromium browser via Playwright and scrape a local
HTML file that replicates ProtonMail's filter page structure.
"""

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
from playwright.async_api import async_playwright

from src.scraper.protonmail_scraper import ProtonMailScraper, _distribute_indices
from src.scraper.browser import FILTERS_PAGE_LOAD_MS

MOCK_HTML = Path(__file__).parent / "fixtures" / "mock_filters_page.html"
MOCK_URL = f"file://{MOCK_HTML.resolve()}"

# Expected filter names in priority order
EXPECTED_NAMES = [
    "Newsletter Trash",
    "Work Emails",
    "Project Updates",
    "VIP Senders",
    "Spam Filter",
    "Finance Reports",
    "Read Receipts",
    "Personal Mail",
    "Disabled Old Filter",
    "Multi-tag Filter",
]


@pytest.fixture
def mock_scraper():
    """Create a scraper instance wired to the mock HTML page."""
    scraper = ProtonMailScraper(headless=True)
    return scraper


async def _setup_scraper_on_mock(scraper: ProtonMailScraper):
    """Initialize browser and navigate to mock page."""
    await scraper.initialize()
    await scraper.page.goto(MOCK_URL, wait_until="domcontentloaded")
    await scraper.page.wait_for_timeout(500)


@pytest.mark.asyncio
async def test_sequential_scraping(mock_scraper):
    """Test sequential scraping (workers=1) against mock page."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)
        filters = await scraper.scrape_all_filters(workers=1)

        assert len(filters) == 10
        names = [f["name"] for f in filters]
        assert names == EXPECTED_NAMES

        # Check priority ordering
        for i, f in enumerate(filters):
            assert f["priority"] == i
    finally:
        await scraper.close()


@pytest.mark.asyncio
async def test_parallel_scraping_3_workers(mock_scraper):
    """Test parallel scraping with 3 workers against mock page."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)
        filters = await scraper.scrape_all_filters(workers=3)

        assert len(filters) == 10
        names = [f["name"] for f in filters]
        assert names == EXPECTED_NAMES

        # Verify priority ordering preserved after merge
        for i, f in enumerate(filters):
            assert f["priority"] == i
    finally:
        await scraper.close()


@pytest.mark.asyncio
async def test_parallel_scraping_5_workers(mock_scraper):
    """Test parallel scraping with 5 workers (more workers than sensible)."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)
        filters = await scraper.scrape_all_filters(workers=5)

        assert len(filters) == 10
        names = [f["name"] for f in filters]
        assert names == EXPECTED_NAMES
    finally:
        await scraper.close()


@pytest.mark.asyncio
async def test_conditions_parsed(mock_scraper):
    """Verify conditions are correctly scraped from mock page."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)
        filters = await scraper.scrape_all_filters(workers=1)

        # Filter 0: Newsletter Trash - sender contains newsletter@example.com
        f0 = filters[0]
        assert len(f0["conditions"]) == 1
        assert f0["conditions"][0]["type"] == "sender"
        assert f0["conditions"][0]["operator"] == "contains"
        assert "newsletter@example.com" in f0["conditions"][0]["value"]

        # Filter 3: VIP Senders - two sender conditions with "any" logic
        f3 = filters[3]
        assert f3["logic"] == "or"
        assert len(f3["conditions"]) == 2

        # Filter 5: Finance Reports - two conditions
        f5 = filters[5]
        assert len(f5["conditions"]) == 2
        assert f5["conditions"][0]["type"] == "sender"
        assert f5["conditions"][1]["operator"] == "starts_with"
    finally:
        await scraper.close()


@pytest.mark.asyncio
async def test_actions_parsed(mock_scraper):
    """Verify actions are correctly scraped from mock page."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)
        filters = await scraper.scrape_all_filters(workers=1)

        # Filter 0: Newsletter Trash -> Trash = delete action
        f0 = filters[0]
        assert any(a["type"] == "delete" for a in f0["actions"])

        # Filter 2: Project Updates -> mark_read
        f2 = filters[2]
        assert any(a["type"] == "mark_read" for a in f2["actions"])

        # Filter 3: VIP Senders -> starred
        f3 = filters[3]
        assert any(a["type"] == "star" for a in f3["actions"])

        # Filter 9: Multi-tag -> mark_read AND starred
        f9 = filters[9]
        action_types = [a["type"] for a in f9["actions"]]
        assert "mark_read" in action_types
        assert "star" in action_types
    finally:
        await scraper.close()


@pytest.mark.asyncio
async def test_enabled_state(mock_scraper):
    """Verify enabled/disabled state is correctly read."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)
        filters = await scraper.scrape_all_filters(workers=1)

        enabled_states = {f["name"]: f["enabled"] for f in filters}
        assert enabled_states["Spam Filter"] is False
        assert enabled_states["Disabled Old Filter"] is False
        assert enabled_states["Newsletter Trash"] is True
        assert enabled_states["Work Emails"] is True
    finally:
        await scraper.close()


@pytest.mark.asyncio
async def test_folder_path_resolution(mock_scraper):
    """Verify nested folder paths are resolved correctly."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)
        filters = await scraper.scrape_all_filters(workers=1)

        # Filter 2: Project Updates -> "• Projects" should resolve to "Work/Projects"
        f2 = filters[2]
        folder_actions = [a for a in f2["actions"] if a["type"] == "move_to"]
        if folder_actions:
            assert folder_actions[0]["parameters"]["folder"] == "Work/Projects"

        # Filter 5: Finance Reports -> "• Finance" should resolve to "Personal/Finance"
        f5 = filters[5]
        folder_actions = [a for a in f5["actions"] if a["type"] == "move_to"]
        if folder_actions:
            assert folder_actions[0]["parameters"]["folder"] == "Personal/Finance"
    finally:
        await scraper.close()


@pytest.mark.asyncio
async def test_parallel_matches_sequential(mock_scraper):
    """Verify parallel results exactly match sequential results."""
    scraper = mock_scraper
    try:
        await _setup_scraper_on_mock(scraper)

        # Run sequential
        seq_filters = await scraper.scrape_all_filters(workers=1)

        # Reset folder map so parallel path builds its own
        scraper._folder_path_map = None

        # Navigate main page back (parallel workers use their own pages)
        await scraper.page.goto(MOCK_URL, wait_until="domcontentloaded")
        await scraper.page.wait_for_timeout(500)

        par_filters = await scraper.scrape_all_filters(workers=3)

        assert len(seq_filters) == len(par_filters)
        for s, p in zip(seq_filters, par_filters):
            assert s["name"] == p["name"]
            assert s["priority"] == p["priority"]
            assert s["enabled"] == p["enabled"]
            assert s["logic"] == p["logic"]
    finally:
        await scraper.close()
