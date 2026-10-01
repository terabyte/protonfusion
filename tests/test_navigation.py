"""Integration tests for filter-page navigation against the mock HTML page.

Launches headless Chromium but never reaches the network: requests to
account.proton.me are answered locally from tests/fixtures via route
interception, so the real goto/wait/assert path runs against a known page.
"""

from pathlib import Path

import pytest

from src.scraper.browser import ProtonMailBrowser

pytestmark = pytest.mark.integration

MOCK_HTML = (Path(__file__).parent / "fixtures" / "mock_filters_page.html").read_text()
WRONG_PAGE_HTML = "<html><body><div class='container-section-sticky'><h1>Inbox</h1></div></body></html>"


async def _browser_serving(html: str, requested: list) -> ProtonMailBrowser:
    """A headless browser whose account.proton.me requests all return `html`."""
    browser = ProtonMailBrowser(headless=True)
    await browser.initialize()

    async def handler(route):
        requested.append(route.request.url)
        await route.fulfill(status=200, content_type="text/html", body=html)

    await browser.context.route("https://account.proton.me/**", handler)
    return browser


@pytest.mark.asyncio
async def test_direct_navigation_lands_on_filters_page():
    requested = []
    browser = await _browser_serving(MOCK_HTML, requested)
    try:
        browser.account_slot = 1
        await browser.navigate_to_filters()
        assert requested == ["https://account.proton.me/u/1/mail/filters"]
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_direct_navigation_rejects_wrong_page(monkeypatch):
    requested = []
    browser = await _browser_serving(WRONG_PAGE_HTML, requested)
    monkeypatch.setattr("src.scraper.browser.FILTERS_PAGE_WAIT_MS", 500)
    try:
        with pytest.raises(Exception):
            await browser._open_filters_directly()
    finally:
        await browser.close()
