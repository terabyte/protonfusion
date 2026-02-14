"""E2E test fixtures: login once per session via ProtonMailBrowser."""

import pytest
import pytest_asyncio

from src.utils.config import load_credentials
from src.scraper.browser import ProtonMailBrowser


@pytest_asyncio.fixture(scope="session")
async def protonmail_page(request):
    """Session-scoped fixture that logs in to ProtonMail and navigates to filters.

    Skips all e2e tests when --credentials-file is not provided.
    Yields the Playwright page object positioned on the filters page.
    """
    creds_path = request.config.getoption("--credentials-file")
    if not creds_path:
        pytest.skip("--credentials-file not provided; skipping e2e tests")

    credentials = load_credentials(creds_path)
    browser = ProtonMailBrowser(headless=True, credentials=credentials)

    await browser.initialize()
    await browser.login()
    await browser.navigate_to_filters()

    yield browser

    await browser.close()
