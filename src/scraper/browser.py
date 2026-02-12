"""Shared browser automation base class for ProtonMail."""

import logging
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from src.scraper import selectors
from src.utils.config import (
    Credentials,
    PROTONMAIL_LOGIN_URL,
    LOGIN_TIMEOUT_MS, PAGE_LOAD_TIMEOUT_MS, ELEMENT_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

# Browser configuration
VIEWPORT = {"width": 1280, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Navigation timeouts (ms)
COMPOSE_WAIT_MS = 30000
SETTINGS_DRAWER_MS = 2000
ALL_SETTINGS_LOAD_MS = 5000
FILTERS_PAGE_LOAD_MS = 3000
MODAL_TRANSITION_MS = 1500
DROPDOWN_MS = 500

INBOX_URL = "https://mail.proton.me/u/0/inbox"


class ProtonMailBrowser:
    """Base class for ProtonMail browser automation.

    Handles initialization, login, and navigation to filters page.
    Subclassed by ProtonMailScraper (read operations) and
    ProtonMailSync (write operations).
    """

    def __init__(self, headless: bool = False, credentials: Optional[Credentials] = None):
        self.headless = headless
        self.credentials = credentials
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None

    async def initialize(self):
        """Launch Playwright browser."""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
        )
        self.page = await self.context.new_page()
        logger.info("Browser initialized (headless=%s)", self.headless)

    async def login(self) -> bool:
        """Login to ProtonMail.

        Uses stored credentials if available, otherwise waits for manual login.
        """
        page = self.page
        await page.goto(
            PROTONMAIL_LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_TIMEOUT_MS,
        )
        logger.info("Navigated to login page")

        if self.credentials:
            return await self._automated_login()
        else:
            return await self._manual_login()

    async def _automated_login(self) -> bool:
        """Login automatically using stored credentials."""
        page = self.page
        try:
            await page.wait_for_selector(selectors.USERNAME_INPUT, timeout=ELEMENT_TIMEOUT_MS)
            await page.fill(selectors.USERNAME_INPUT, self.credentials.username)
            logger.info("Filled username: %s", self.credentials.username)

            await page.click(selectors.LOGIN_BUTTON)

            await page.wait_for_selector(selectors.PASSWORD_INPUT, timeout=ELEMENT_TIMEOUT_MS)
            await page.fill(selectors.PASSWORD_INPUT, self.credentials.password)
            logger.info("Filled password")

            await page.click(selectors.LOGIN_BUTTON)

            await page.wait_for_url(
                lambda url: "/mail/" in url or "/apps" in url,
                timeout=LOGIN_TIMEOUT_MS,
            )
            logger.info("Login successful (redirected to: %s)", page.url)
            return True

        except Exception as e:
            raise RuntimeError(f"Automated login failed: {e}. Check credentials file.")

    async def _manual_login(self) -> bool:
        """Wait for user to manually login."""
        page = self.page
        logger.info("Waiting for manual login...")
        print("\n>>> Please log in to ProtonMail in the browser window. <<<\n")

        try:
            await page.wait_for_url(
                lambda url: "/mail/" in url or "/apps" in url,
                timeout=LOGIN_TIMEOUT_MS,
            )
            logger.info("Manual login detected (redirected to: %s)", page.url)
            return True
        except Exception:
            raise RuntimeError("Login timed out. Please try again.")

    async def navigate_to_filters(self):
        """Navigate to filter settings page via the UI.

        ProtonMail settings are at account.proton.me. We navigate by:
        1. Ensuring the mail app is loaded
        2. Clicking the settings gear icon
        3. Clicking "All settings"
        4. Clicking "Filters" in the sidebar
        """
        page = self.page

        await page.goto(
            INBOX_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_TIMEOUT_MS,
        )
        await page.wait_for_selector(selectors.COMPOSE_BUTTON, timeout=COMPOSE_WAIT_MS)

        await page.click(selectors.SETTINGS_GEAR)
        await page.wait_for_timeout(SETTINGS_DRAWER_MS)

        all_settings = await page.query_selector(selectors.ALL_SETTINGS_LINK)
        if all_settings:
            await all_settings.click()
            await page.wait_for_timeout(ALL_SETTINGS_LOAD_MS)
        else:
            raise RuntimeError("Could not find 'All settings' link")

        filters_link = await page.query_selector(selectors.FILTERS_NAV_LINK)
        if filters_link:
            await filters_link.click()
            await page.wait_for_timeout(FILTERS_PAGE_LOAD_MS)
        else:
            raise RuntimeError("Could not find 'Filters' link in settings sidebar")

        logger.info("Navigated to filter settings at %s", page.url)

    async def read_sieve_script(self, filter_name: str = "") -> str:
        """Read an existing Sieve filter's script from ProtonMail.

        If filter_name is provided, looks for that named filter and opens it.
        Otherwise, opens the "Add sieve filter" modal to read the default content.
        Returns the script text, or empty string if not found.
        """
        page = self.page

        try:
            opened = False

            if filter_name:
                opened = await self._open_sieve_filter_by_name(filter_name)
                if not opened:
                    logger.info("Sieve filter '%s' not found", filter_name)
                    return ""
            else:
                # Try "Add sieve filter" button (only works if filter slot is available)
                add_btn = await page.query_selector(selectors.ADD_SIEVE_FILTER_BUTTON)
                if add_btn and await add_btn.is_visible():
                    await add_btn.click()
                    await page.wait_for_timeout(ALL_SETTINGS_LOAD_MS)
                    opened = True
                else:
                    logger.info("No sieve filter to read (Add button not available)")
                    return ""

            if not opened:
                return ""

            # Wait for CodeMirror to initialize
            try:
                await page.wait_for_selector(
                    selectors.SIEVE_EDITOR_CM, timeout=ELEMENT_TIMEOUT_MS,
                )
            except Exception:
                logger.warning("CodeMirror editor not found in Sieve modal")
                return ""

            # Read content via CodeMirror 5 API
            content = await page.evaluate(
                "() => { const cm = document.querySelector('.CodeMirror'); "
                "return cm && cm.CodeMirror ? cm.CodeMirror.getValue() : ''; }"
            )

            # Close the modal without saving
            close_btn = await page.query_selector(
                f'{selectors.FILTER_MODAL_CLOSE}, {selectors.CANCEL_BUTTON}'
            )
            if close_btn:
                await close_btn.click()
                await page.wait_for_timeout(MODAL_TRANSITION_MS)

            script = (content or "").strip()
            logger.info(
                "Read Sieve script: %d chars, %d lines",
                len(script),
                script.count("\n") + 1 if script else 0,
            )
            return script

        except Exception as e:
            logger.error("Failed to read Sieve script: %s", e)
            return ""

    async def _open_sieve_filter_by_name(self, name: str) -> bool:
        """Find a filter by name in the list and click its Edit button.

        Returns True if the filter was found and the edit modal was opened.
        Scoped to the Custom filters section only.
        """
        page = self.page
        section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
        if not section:
            logger.warning("Custom filters section not found")
            return False
        rows = await section.query_selector_all(selectors.FILTER_TABLE_ROWS)

        for row in rows:
            # Check the Edit button aria-label for the name
            edit_btn = await row.query_selector(selectors.FILTER_EDIT_BUTTON)
            if edit_btn:
                aria = await edit_btn.get_attribute("aria-label")
                if aria and name in aria:
                    await edit_btn.click()
                    await page.wait_for_timeout(ALL_SETTINGS_LOAD_MS)
                    return True

            # Fallback: check cell text
            tds = await row.query_selector_all("td")
            for td in tds:
                text = (await td.inner_text()).strip()
                if text == name:
                    edit_btn = await row.query_selector(
                        f'{selectors.FILTER_EDIT_BUTTON}, {selectors.FILTER_EDIT_BUTTON_ALT}'
                    )
                    if edit_btn:
                        await edit_btn.click()
                        await page.wait_for_timeout(ALL_SETTINGS_LOAD_MS)
                        return True

        return False

    async def close(self):
        """Close the browser."""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")
