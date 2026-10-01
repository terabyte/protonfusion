"""Shared browser automation base class for ProtonMail."""

import json
import logging
import os
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from src.scraper import selectors
from src.utils.config import (
    Credentials,
    PROTONMAIL_LOGIN_URL, MAIL_HOST, ACCOUNT_HOST, INBOX_PATH, FILTERS_PATH,
    DEFAULT_ACCOUNT_SLOT, proton_url, slot_from_url, resolve_storage_state_path,
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

# How long a saved session gets to open the mail app before it counts as dead.
SESSION_CHECK_MS = 30000
# Our own key inside the saved storage-state JSON (Playwright ignores the file's
# other contents only if we strip this before handing the state over).
STATE_META_KEY = "protonfusion"


def write_private_file(path: Path, text: str):
    """Write text to path readable only by the owner.

    Any directories created are 0700 and the file is 0600 (via umask), and the
    write goes through a temp file + rename so a crash never leaves half a file.
    """
    old_umask = os.umask(0o077)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old_umask)


class ProtonMailBrowser:
    """Base class for ProtonMail browser automation.

    Handles initialization, login, and navigation to filters page.
    Subclassed by ProtonMailScraper (read operations) and
    ProtonMailSync (write operations).
    """

    def __init__(
        self,
        headless: bool = False,
        credentials: Optional[Credentials] = None,
        storage_state_path: Optional[Union[str, Path]] = None,
    ):
        self.headless = headless
        self.credentials = credentials
        # Saved session file; see resolve_storage_state_path for the precedence.
        self.storage_state_path: Path = resolve_storage_state_path(
            str(storage_state_path) if storage_state_path else None
        )
        self.session_loaded = False  # a saved session was put into the context
        self._save_state_on_close = False
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

    async def initialize(self, load_storage_state: bool = True):
        """Launch Playwright browser, preloading the saved session if there is one.

        load_storage_state=False starts clean (used by the `login` command).
        """
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(headless=self.headless)
        context_options = {"viewport": VIEWPORT, "user_agent": USER_AGENT}
        state = self._load_storage_state() if load_storage_state else None
        try:
            self.context = await self.browser.new_context(
                **context_options, **({"storage_state": state} if state else {})
            )
        except Exception as e:
            if not state:
                raise
            logger.warning("Saved session at %s was rejected (%s); starting fresh", self.storage_state_path, e)
            self.session_loaded = False
            self.context = await self.browser.new_context(**context_options)
        self.page = await self.context.new_page()
        logger.info("Browser initialized (headless=%s)", self.headless)

    def _load_storage_state(self) -> Optional[dict]:
        """Read the saved session file, or None if absent or unreadable.

        Also restores the account slot recorded when the session was saved, so
        session reuse goes straight to the right /u/<slot>/.
        """
        path = self.storage_state_path
        if not path.exists():
            return None
        try:
            state = json.loads(path.read_text())
            if not isinstance(state, dict):
                raise ValueError("not a storage-state object")
        except (OSError, ValueError) as e:
            logger.warning("Ignoring unreadable saved session %s: %s", path, e)
            return None
        meta = state.pop(STATE_META_KEY, None)
        meta = meta if isinstance(meta, dict) else {}
        slot = meta.get("account_slot")
        if isinstance(slot, int) and slot >= 0:
            self.account_slot = slot
        self.session_loaded = True
        logger.info("Loading saved session from %s", path)
        return state

    async def save_storage_state(self, path: Optional[Path] = None) -> Path:
        """Save the context's cookies + localStorage (owner-only) and return the path.

        The file holds live auth cookies: it is written 0600 in a 0700 directory
        and its contents are never logged.
        """
        path = Path(path) if path else self.storage_state_path
        state = await self.context.storage_state()
        state[STATE_META_KEY] = {"account_slot": self.account_slot}
        write_private_file(path, json.dumps(state))
        logger.info("Saved browser session to %s", path)
        return path

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
        if not self.session_loaded:
            return False
        page = self.page
        await page.goto(self.mail_url(INBOX_PATH), wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        try:
            await page.wait_for_selector(selectors.COMPOSE_BUTTON, timeout=SESSION_CHECK_MS)
        except Exception:
            logger.warning("Saved session did not reach the mail app (%s); logging in normally", page.url)
            return False
        logger.info("Reused saved session")
        # Proton may rotate tokens during the run; write them back on close so
        # the saved session stays usable.
        self._save_state_on_close = True
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
        """Close the browser, first refreshing the saved session if one was reused."""
        if self._save_state_on_close and self.context:
            try:
                await self.save_storage_state()
            except Exception as e:
                logger.warning("Could not refresh saved session: %s", e)
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")
