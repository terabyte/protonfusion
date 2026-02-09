"""Playwright automation for scraping ProtonMail filters."""

import asyncio
import logging
from typing import List, Optional

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from src.scraper import selectors
from src.utils.config import (
    Credentials, load_credentials,
    PROTONMAIL_LOGIN_URL, PROTONMAIL_SETTINGS_FILTERS_URL,
    LOGIN_TIMEOUT_MS, PAGE_LOAD_TIMEOUT_MS, ELEMENT_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)


class ProtonMailScraper:
    """Scrapes filters from ProtonMail settings UI."""

    def __init__(self, headless: bool = False, credentials: Optional[Credentials] = None):
        self.headless = headless
        self.credentials = credentials
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def initialize(self):
        """Launch Playwright browser."""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()
        logger.info("Browser initialized (headless=%s)", self.headless)

    async def login(self) -> bool:
        """Login to ProtonMail. Uses credentials if provided, otherwise waits for manual login."""
        page = self.page
        await page.goto(PROTONMAIL_LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        logger.info("Navigated to login page")

        if self.credentials:
            return await self._automated_login()
        else:
            return await self._manual_login()

    async def _automated_login(self) -> bool:
        """Login automatically using stored credentials."""
        page = self.page
        try:
            # Fill username
            await page.wait_for_selector(selectors.USERNAME_INPUT, timeout=ELEMENT_TIMEOUT_MS)
            await page.fill(selectors.USERNAME_INPUT, self.credentials.username)
            logger.info("Filled username: %s", self.credentials.username)

            # Click submit to proceed to password
            await page.click(selectors.LOGIN_BUTTON)

            # Fill password
            await page.wait_for_selector(selectors.PASSWORD_INPUT, timeout=ELEMENT_TIMEOUT_MS)
            await page.fill(selectors.PASSWORD_INPUT, self.credentials.password)
            logger.info("Filled password")

            # Submit login
            await page.click(selectors.LOGIN_BUTTON)

            # Wait for navigation away from login page (may go to /apps or /mail)
            await page.wait_for_url(
                lambda url: "/mail/" in url or "/apps" in url,
                timeout=LOGIN_TIMEOUT_MS
            )
            logger.info("Login successful (redirected to: %s)", page.url)
            return True

        except Exception as e:
            logger.error("Automated login failed: %s", e)
            raise RuntimeError(f"Automated login failed: {e}. Check credentials file.")

    async def _manual_login(self) -> bool:
        """Wait for user to manually login."""
        page = self.page
        logger.info("Waiting for manual login... Please log in to ProtonMail in the browser window.")
        print("\n>>> Please log in to ProtonMail in the browser window. <<<\n")

        try:
            await page.wait_for_url(
                lambda url: "/mail/" in url or "/apps" in url,
                timeout=LOGIN_TIMEOUT_MS
            )
            logger.info("Manual login detected (redirected to: %s)", page.url)
            return True
        except Exception:
            raise RuntimeError("Login timed out. Please try again.")

    async def navigate_to_filters(self):
        """Navigate to the filter settings page via the UI.

        ProtonMail settings are now at account.proton.me. We navigate by:
        1. Ensuring the mail app is loaded
        2. Clicking the settings gear icon
        3. Clicking "All settings"
        4. Clicking "Filters" in the sidebar
        """
        page = self.page

        # Ensure the mail app is loaded first
        await page.goto("https://mail.proton.me/u/0/inbox",
                        wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_selector(selectors.COMPOSE_BUTTON, timeout=30000)

        # Open settings drawer
        await page.click(selectors.SETTINGS_GEAR)
        await page.wait_for_timeout(2000)

        # Click "All settings" link
        all_settings = await page.query_selector(selectors.ALL_SETTINGS_LINK)
        if all_settings:
            await all_settings.click()
            await page.wait_for_timeout(5000)
        else:
            raise RuntimeError("Could not find 'All settings' link")

        # Click "Filters" in the sidebar
        filters_link = await page.query_selector(selectors.FILTERS_NAV_LINK)
        if filters_link:
            await filters_link.click()
            await page.wait_for_timeout(3000)
        else:
            raise RuntimeError("Could not find 'Filters' link in settings sidebar")

        logger.info("Navigated to filter settings at %s", page.url)

    async def scrape_all_filters(self) -> List[dict]:
        """Scrape all filters from the settings page.

        Returns list of dicts with filter data. Each dict has:
        - name: str
        - enabled: bool
        - conditions: list of dicts
        - actions: list of dicts
        """
        page = self.page
        filters = []

        # Get all filter items - ProtonMail uses a <table> for the filter list
        filter_items = await page.query_selector_all('table.simple-table tbody tr')
        total = len(filter_items)
        logger.info("Found %d filter items", total)

        if total == 0:
            # Try alternative selectors
            filter_items = await page.query_selector_all(
                '[class*="filter"] [class*="item"], '
                '[class*="filters-list"] > div, '
                '.item-container'
            )
            total = len(filter_items)
            logger.info("Retry found %d filter items", total)

        for idx, item in enumerate(filter_items):
            try:
                filter_data = await self._scrape_single_filter(item, idx)
                if filter_data:
                    filters.append(filter_data)
                    logger.info("Scraped filter %d/%d: %s", idx + 1, total, filter_data.get("name", "Unknown"))
            except Exception as e:
                logger.warning("Failed to scrape filter %d: %s", idx, e)

        return filters

    async def _scrape_single_filter(self, item, idx: int) -> Optional[dict]:
        """Scrape a single filter item from the list."""
        page = self.page

        # Get filter name from the table row
        # New UI: filter name is in a <td> cell, or via the Edit button's aria-label
        name = ""
        edit_btn = await item.query_selector('button[aria-label*="Edit filter"]')
        if edit_btn:
            aria = await edit_btn.get_attribute("aria-label")
            # aria-label is like 'Edit filter "My Filter"'
            if aria and '"' in aria:
                name = aria.split('"')[1]
        if not name:
            # Fallback: get text from the second <td> (first is drag handle)
            tds = await item.query_selector_all("td")
            if len(tds) >= 2:
                name = (await tds[1].inner_text()).strip()
            elif tds:
                name = (await tds[0].inner_text()).strip()
        if not name:
            # Last fallback
            name_el = await item.query_selector('.text-ellipsis, [title], span')
            name = await name_el.inner_text() if name_el else f"Filter {idx}"
            name = name.strip()

        # Get enabled state from toggle
        toggle_input = await item.query_selector('input[type="checkbox"], .toggle-label input')
        enabled = True
        if toggle_input:
            enabled = await toggle_input.is_checked()

        # Try to get conditions/actions by opening the edit wizard
        conditions = []
        actions = []
        logic = "and"

        try:
            # Click "Edit" button to open the filter wizard
            edit_btn = await item.query_selector(
                'button[aria-label*="Edit filter"], button:has-text("Edit")'
            )
            if edit_btn:
                await edit_btn.click()
                await page.wait_for_timeout(1500)

                # The wizard opens on the Name step - click Next to go to Conditions
                next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
                if next_btn:
                    await next_btn.click()
                    await page.wait_for_timeout(1500)

                    # Scrape conditions from the Conditions step
                    conditions = await self._scrape_conditions()
                    logic = await self._scrape_logic()

                    # Click Next to go to Actions step
                    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
                    if next_btn:
                        await next_btn.click()
                        await page.wait_for_timeout(1500)
                        actions = await self._scrape_actions()

                # Close modal via Cancel/Close button
                close_btn = await page.query_selector(
                    selectors.FILTER_MODAL_CLOSE
                    + ', button:has-text("Cancel")'
                )
                if close_btn:
                    await close_btn.click()
                    await page.wait_for_timeout(500)
        except Exception as e:
            logger.debug("Could not open edit modal for filter '%s': %s", name, e)

        return {
            "name": name,
            "enabled": enabled,
            "priority": idx,
            "logic": logic,
            "conditions": conditions,
            "actions": actions,
        }

    async def _scrape_conditions(self) -> List[dict]:
        """Scrape conditions from the Conditions step of the filter wizard."""
        page = self.page
        conditions = []

        # New UI uses data-testid="filter-modal:condition-N" for each row
        condition_rows = await page.query_selector_all('[data-testid*="filter-modal:condition"]')

        for row in condition_rows:
            try:
                # Condition type and comparator are button.select elements
                select_btns = await row.query_selector_all('button.select')
                cond_type = "subject"
                operator = "contains"

                if len(select_btns) >= 1:
                    label = await select_btns[0].get_attribute("aria-label")
                    if label:
                        cond_type = label.lower().strip()

                if len(select_btns) >= 2:
                    label = await select_btns[1].get_attribute("aria-label")
                    if label:
                        operator = label.lower().strip()

                # Map UI labels back to our model values
                type_map = {
                    "the sender": "sender",
                    "the recipient": "recipient",
                    "the subject": "subject",
                    "the attachment": "attachments",
                }
                cond_type = type_map.get(cond_type, cond_type)

                operator_map = {
                    "is exactly": "is",
                    "begins with": "starts_with",
                    "ends with": "ends_with",
                }
                operator = operator_map.get(operator, operator)

                # Get values - they may be tags/chips or input value
                value = ""
                # Check for inserted value tags (shown as chips after Insert)
                tags = await row.query_selector_all('[class*="tag"], [class*="chip"], [class*="pill"]')
                if tags:
                    tag_texts = []
                    for tag in tags:
                        tag_texts.append((await tag.inner_text()).strip())
                    value = ", ".join(tag_texts)
                else:
                    # Fallback: check text input
                    value_el = await row.query_selector('input[type="text"]')
                    if value_el:
                        value = await value_el.input_value()

                conditions.append({
                    "type": cond_type,
                    "operator": operator,
                    "value": value.strip(),
                })
            except Exception as e:
                logger.debug("Failed to scrape condition: %s", e)

        return conditions

    async def _scrape_actions(self) -> List[dict]:
        """Scrape actions from the Actions step of the filter wizard.

        The Actions step has separate sections: Move to, Label as, Mark as.
        """
        page = self.page
        actions = []

        # Check "Move to" folder selection
        folder_row = await page.query_selector(selectors.FILTER_ACTION_FOLDER_ROW)
        if folder_row:
            folder_btn = await folder_row.query_selector('button.select')
            if folder_btn:
                label = await folder_btn.get_attribute("aria-label")
                if label and label != "Do not move":
                    folder_map = {
                        "Trash": "delete",
                        "Archive": "archive",
                        "Spam": "move_to",
                        "Inbox - Default": "move_to",
                    }
                    action_type = folder_map.get(label, "move_to")
                    if action_type in ("delete", "archive"):
                        actions.append({"type": action_type, "parameters": {}})
                    else:
                        actions.append({"type": "move_to", "parameters": {"folder": label}})

        # Check "Mark as" checkboxes
        mark_row = await page.query_selector(selectors.FILTER_ACTION_MARK_AS_ROW)
        if mark_row:
            read_cb = await mark_row.query_selector('label:has-text("Read") input[type="checkbox"]')
            if read_cb and await read_cb.is_checked():
                actions.append({"type": "mark_read", "parameters": {}})
            star_cb = await mark_row.query_selector('label:has-text("Starred") input[type="checkbox"]')
            if star_cb and await star_cb.is_checked():
                actions.append({"type": "star", "parameters": {}})

        return actions

    async def _scrape_logic(self) -> str:
        """Scrape the logic type (AND/OR) from the Conditions step."""
        page = self.page
        try:
            # Check which radio button is selected (ALL vs ANY)
            any_radio = await page.query_selector('input[type="radio"]:checked')
            if any_radio:
                # Check the label text near the checked radio
                label = await any_radio.evaluate(
                    'el => el.closest("label")?.textContent || ""'
                )
                if "any" in label.lower():
                    return "or"
        except Exception:
            pass
        return "and"

    async def close(self):
        """Close the browser."""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")
