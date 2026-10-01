"""Raw step evidence comes only from the wizard modal, never the whole page."""

import pytest

from src.scraper.protonmail_scraper import ProtonMailScraper

PAGE = """
<html><body>
  <h1>Filters</h1>
  <input aria-label="Search settings" value="page-wide secret">
  <button id="outside">Next</button>
  <div class="modal-content">
    <p>Conditions</p>
    <input aria-label="Sender" value="news@example.com">
    <button id="inside">Next</button>
  </div>
</body></html>
"""


class NoAnchorPage:
    """A page where no anchor selector matches; evaluating the body would fail the test."""

    async def query_selector(self, selector):
        return None

    async def evaluate(self, js):
        raise AssertionError("must not fall back to the page body")


@pytest.mark.asyncio
async def test_no_anchor_returns_empty_without_reading_body():
    scraper = ProtonMailScraper(headless=True)
    assert await scraper._read_step_text(NoAnchorPage(), ["#a", "#b"]) == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anchor_outside_modal_returns_empty_inside_returns_modal_only():
    scraper = ProtonMailScraper(headless=True)
    try:
        await scraper.initialize(load_storage_state=False)
        await scraper.page.set_content(PAGE)

        assert await scraper._read_step_text(scraper.page, ["#outside"]) == ""

        text = await scraper._read_step_text(scraper.page, ["#inside"])
        assert "Conditions" in text
        assert "[input] Sender = news@example.com" in text
        assert "page-wide secret" not in text
        assert "Filters" not in text
    finally:
        await scraper.close()
