"""Shared browser automation base class for ProtonMail."""

import logging
import os
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from src.scraper import selectors
from src.utils.config import (
    Credentials,
    PROTONMAIL_LOGIN_URL, MAIL_HOST, ACCOUNT_HOST, INBOX_PATH, FILTERS_PATH,
    DEFAULT_ACCOUNT_SLOT, proton_url, slot_from_url,
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
FILTERS_PAGE_WAIT_MS = 30000
MODAL_TRANSITION_MS = 1500
DROPDOWN_MS = 500
POST_LOGIN_SETTLE_MS = 15000
MAX_ONBOARDING_MODALS = 6

# Saved Playwright session (cookies + localStorage) from a prior human login.
# Proton puts a CAPTCHA in front of automated logins, so the practical way to run
# headless is: a human logs in once in a visible browser, the session is saved,
# and later runs reuse it until Proton expires it. See docs/plan-session-management.md.
STORAGE_STATE_ENV = "PROTONFUSION_STORAGE_STATE"
SESSION_CHECK_MS = 30000


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
        self.account_email: str = ""
        # Session slot from the /u/<slot>/ part of Proton URLs; updated from the
        # URL the browser lands on after login or session reuse.
        self.account_slot: int = DEFAULT_ACCOUNT_SLOT

    def mail_url(self, path: str) -> str:
        """URL of a mail.proton.me page for this account's session slot."""
        return proton_url(MAIL_HOST, path, self.account_slot)

    def account_url(self, path: str) -> str:
        """URL of an account.proton.me page for this account's session slot."""
        return proton_url(ACCOUNT_HOST, path, self.account_slot)

    def _record_account_slot(self):
        """Adopt the session slot from the current page URL, if it has one."""
        slot = slot_from_url(self.page.url)
        if slot is not None and slot != self.account_slot:
            logger.info("Account session slot is /u/%d/", slot)
            self.account_slot = slot

    async def initialize(self):
        """Launch Playwright browser."""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(headless=self.headless)
        self.storage_state_path = os.environ.get(STORAGE_STATE_ENV, "")
        context_options = {"viewport": VIEWPORT, "user_agent": USER_AGENT}
        if self.storage_state_path and os.path.exists(self.storage_state_path):
            context_options["storage_state"] = self.storage_state_path
            logger.info("Loading saved session from %s", self.storage_state_path)
        self.context = await self.browser.new_context(**context_options)
        self.page = await self.context.new_page()
        logger.info("Browser initialized (headless=%s)", self.headless)

    async def login(self) -> bool:
        """Login to ProtonMail.

        Uses stored credentials if available, otherwise waits for manual login.
        """
        page = self.page
        if not await self._reuse_saved_session():
            await page.goto(
                PROTONMAIL_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=PAGE_LOAD_TIMEOUT_MS,
            )
            logger.info("Navigated to login page")

            if self.credentials:
                await self._automated_login()
            else:
                await self._manual_login()
        await self._after_login()
        return True

    async def _after_login(self):
        """Settle into the signed-in app: record the slot, clear onboarding, read the email.

        Best-effort throughout; login has already succeeded by the time this runs.
        """
        self._record_account_slot()
        if urlparse(self.page.url).hostname == MAIL_HOST:
            try:
                await self.page.wait_for_selector(selectors.COMPOSE_BUTTON, timeout=POST_LOGIN_SETTLE_MS)
            except Exception:
                logger.debug("Mail app did not finish loading after login (%s)", self.page.url)
        await self.dismiss_onboarding_modals()
        if not self.account_email:
            await self._capture_account_email()

    async def dismiss_onboarding_modals(self) -> int:
        """Close first-run modals (e.g. the Welcome tour) that block clicks.

        Best-effort and never raises: returns the number of dismiss actions taken,
        0 when no such modal is showing.
        """
        page = self.page
        dismissed = 0
        try:
            for _ in range(MAX_ONBOARDING_MODALS):
                modal = await page.query_selector(selectors.ONBOARDING_MODAL)
                if not modal or not await modal.is_visible():
                    break
                button = None
                for selector in selectors.ONBOARDING_DISMISS_BUTTONS:
                    candidate = await modal.query_selector(selector)
                    if candidate and await candidate.is_visible():
                        button = candidate
                        break
                if button:
                    await button.click()
                else:
                    await page.keyboard.press("Escape")
                dismissed += 1
                await page.wait_for_timeout(MODAL_TRANSITION_MS)
        except Exception as e:
            logger.debug("Onboarding modal dismissal stopped: %s", e)
        if dismissed:
            logger.info("Dismissed %d onboarding modal step(s)", dismissed)
        return dismissed

    async def _reuse_saved_session(self) -> bool:
        """True if a saved session was loaded and the mail app opens without a login."""
        if not self.storage_state_path or not os.path.exists(self.storage_state_path):
            return False
        page = self.page
        await page.goto(self.mail_url(INBOX_PATH), wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        try:
            await page.wait_for_selector(selectors.COMPOSE_BUTTON, timeout=SESSION_CHECK_MS)
        except Exception:
            logger.warning("Saved session did not reach the mail app (%s); logging in normally", page.url)
            return False
        logger.info("Reused saved session")
        return True

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
        """Open the filter settings page and assert its structure.

        Goes straight to account.proton.me/u/<slot>/mail/filters. The old click
        path through the mail app (gear -> All settings -> Filters) is kept only
        as a fallback for when the direct URL stops working.
        """
        try:
            await self._open_filters_directly()
        except Exception as e:
            logger.warning("Direct navigation to filters failed (%s); trying the settings menu", e)
            await self._navigate_to_filters_via_menu()
            await self._wait_for_filters_page()
            await self._assert_filter_page_structure()
        logger.info("Navigated to filter settings at %s", self.page.url)

    async def _open_filters_directly(self):
        """Load the filters page by URL; raises if it does not render as expected."""
        await self.page.goto(
            self.account_url(FILTERS_PATH),
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_TIMEOUT_MS,
        )
        await self._wait_for_filters_page()
        await self.dismiss_onboarding_modals()
        await self._assert_filter_page_structure()

    async def _wait_for_filters_page(self):
        """Wait until the Custom filters section has rendered."""
        await self.page.wait_for_selector(
            selectors.CUSTOM_FILTERS_HEADING, timeout=FILTERS_PAGE_WAIT_MS,
        )

    async def _capture_account_email(self):
        """Read the account email from the mail app's user dropdown, if present."""
        email_el = await self.page.query_selector(selectors.USER_DROPDOWN_EMAIL)
        if email_el:
            self.account_email = (await email_el.inner_text()).strip()
            logger.info("Account email: %s", self.account_email)

    async def _navigate_to_filters_via_menu(self):
        """Fallback: reach the filters page by clicking through the mail app UI.

        1. Load the mail app inbox
        2. Click the settings gear icon
        3. Click "All settings"
        4. Click "Filters" in the sidebar
        """
        page = self.page

        await page.goto(
            self.mail_url(INBOX_PATH),
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_TIMEOUT_MS,
        )
        await page.wait_for_selector(selectors.COMPOSE_BUTTON, timeout=COMPOSE_WAIT_MS)
        await self.dismiss_onboarding_modals()
        await self._capture_account_email()

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

    async def _assert_filter_page_structure(self):
        """Assert that the filter settings page has the expected structure.

        Fails loudly if ProtonMail changed their UI, rather than silently
        scraping the wrong data.
        """
        page = self.page

        # Page heading
        h1 = await page.query_selector(selectors.PAGE_HEADING)
        if not h1:
            raise RuntimeError("Filter page missing <h1> heading. URL: " + page.url)
        h1_text = (await h1.inner_text()).strip()
        if h1_text != "Filters":
            raise RuntimeError(
                f"Expected h1 'Filters', got {h1_text!r}. "
                "ProtonMail may have changed their settings page."
            )

        # Custom filters section heading
        custom_h2 = await page.query_selector(selectors.CUSTOM_FILTERS_HEADING)
        if not custom_h2:
            raise RuntimeError(
                "Missing 'Custom filters' heading on filters page. "
                "ProtonMail may have changed their UI layout."
            )

        # Spam/allow section heading (must exist so we know we're scoping correctly)
        spam_h2 = await page.query_selector(selectors.SPAM_LISTS_HEADING)
        if not spam_h2:
            raise RuntimeError(
                "Missing 'Spam, block, and allow lists' heading on filters page. "
                "ProtonMail may have changed their UI layout."
            )

        # Add filter button
        add_btn = await page.query_selector(selectors.ADD_FILTER_BUTTON)
        if not add_btn:
            logger.warning("'Add filter' button not found (may be hidden on free tier)")

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

    async def create_worker_page(self) -> Page:
        """Create an additional page in the existing browser context."""
        return await self.context.new_page()

    async def close(self):
        """Close the browser."""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")
