"""Integration tests for filter-page navigation against the mock HTML page.

Launches headless Chromium but never reaches the network: requests to
account.proton.me are answered locally from tests/fixtures via route
interception, so the real goto/wait/assert path runs against a known page.
"""

import json
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


WELCOME_TOUR_HTML = """
<html><body>
<button id="behind">Compose</button>
<div class="modal-two" id="tour">
  <div id="panel1"><h1>Welcome to Proton Mail</h1>
    <button onclick="document.getElementById('panel1').remove();
                     document.getElementById('panel2').style.display='block'">Let’s get started</button>
  </div>
  <div id="panel2" style="display:none"><p>Pick a theme</p>
    <button onclick="document.getElementById('tour').remove()">Skip</button>
  </div>
</div>
</body></html>
"""


@pytest.fixture
def fast_modals(monkeypatch):
    """Skip the real inter-step pause so modal tests stay quick."""
    monkeypatch.setattr("src.scraper.browser.MODAL_TRANSITION_MS", 10)


@pytest.mark.asyncio
async def test_dismisses_multi_step_welcome_tour(fast_modals):
    browser = ProtonMailBrowser(headless=True)
    await browser.initialize()
    try:
        await browser.page.set_content(WELCOME_TOUR_HTML)
        dismissed = await browser.dismiss_onboarding_modals()
        assert dismissed == 2
        assert await browser.page.query_selector("div.modal-two") is None
        await browser.page.click("#behind", timeout=1000)
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_no_modal_is_a_no_op(fast_modals):
    browser = ProtonMailBrowser(headless=True)
    await browser.initialize()
    try:
        await browser.page.set_content("<html><body><p>Inbox</p></body></html>")
        assert await browser.dismiss_onboarding_modals() == 0
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_stubborn_modal_never_raises(fast_modals):
    browser = ProtonMailBrowser(headless=True)
    await browser.initialize()
    try:
        await browser.page.set_content("<div class='modal-two'><p>No buttons here</p></div>")
        dismissed = await browser.dismiss_onboarding_modals()
        assert dismissed > 0  # tried Escape, gave up after the cap, did not raise
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_saved_session_loads_into_real_context(tmp_path):
    """Playwright accepts the saved file once our metadata key is stripped."""
    state_file = tmp_path / "storage_state.json"
    state_file.write_text(json.dumps({
        "cookies": [{
            "name": "probe", "value": "1", "domain": "account.proton.me", "path": "/",
            "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax",
        }],
        "origins": [],
        "protonfusion": {"account_slot": 1},
    }))
    browser = ProtonMailBrowser(headless=True, storage_state_path=state_file)
    await browser.initialize()
    try:
        assert browser.session_loaded
        assert browser.account_slot == 1
        cookies = await browser.context.cookies("https://account.proton.me/")
        assert [c["name"] for c in cookies] == ["probe"]
    finally:
        await browser.close()


ADD_FILTER_WARNING = "'Add filter' button not found"
ADD_FILTER_TAG = '<button id="addFilterBtn">Add filter</button>'


def _mock_page_with(add_filter_markup: str) -> str:
    """The mock filters page with its 'Add filter' button replaced."""
    assert MOCK_HTML.count(ADD_FILTER_TAG) == 1
    return MOCK_HTML.replace(ADD_FILTER_TAG, add_filter_markup)


# Inserts the button 400 ms after load, after the Custom filters heading,
# as the live page did when the check reported it missing.
LATE_ADD_FILTER = """<script>
setTimeout(() => {
  const b = document.createElement('button');
  b.textContent = 'Add filter';
  document.body.prepend(b);
}, 400);
</script>"""


@pytest.mark.asyncio
@pytest.mark.parametrize("markup", [
    LATE_ADD_FILTER,
    # Present but not visible (covered or hidden) still counts as present
    '<button style="display:none">Add filter</button>',
], ids=["renders-late", "hidden"])
async def test_add_filter_check_does_not_warn_when_button_present(markup, caplog):
    browser = await _browser_serving(_mock_page_with(markup), [])
    try:
        browser.account_slot = 1
        with caplog.at_level("WARNING", logger="src.scraper.browser"):
            await browser.navigate_to_filters()
        assert ADD_FILTER_WARNING not in caplog.text
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_add_filter_check_warns_when_button_absent(monkeypatch, caplog):
    monkeypatch.setattr("src.scraper.browser.ADD_FILTER_WAIT_MS", 300)
    browser = await _browser_serving(_mock_page_with(""), [])
    try:
        browser.account_slot = 1
        with caplog.at_level("WARNING", logger="src.scraper.browser"):
            await browser.navigate_to_filters()
        assert ADD_FILTER_WARNING in caplog.text
    finally:
        await browser.close()
