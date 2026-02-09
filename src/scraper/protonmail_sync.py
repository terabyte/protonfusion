"""Playwright automation for sync/restore operations on ProtonMail."""

import asyncio
import logging
from typing import Dict, List, Optional

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from src.scraper import selectors
from src.utils.config import (
    Credentials,
    PROTONMAIL_LOGIN_URL, PROTONMAIL_SETTINGS_FILTERS_URL,
    LOGIN_TIMEOUT_MS, PAGE_LOAD_TIMEOUT_MS, ELEMENT_TIMEOUT_MS,
)

# Maps our model condition types to ProtonMail UI dropdown labels
CONDITION_TYPE_LABELS = {
    "sender": "The sender",
    "recipient": "The recipient",
    "subject": "The subject",
    "attachments": "The attachment",
}

# Maps our model comparators to ProtonMail UI dropdown labels
COMPARATOR_LABELS = {
    "contains": "contains",
    "is": "is exactly",
    "starts_with": "begins with",
    "ends_with": "ends with",
    "matches": "matches",
}

# Maps our model action types to what we do in the Actions step
# The Actions step has separate sections (Move to, Mark as, Label as)
# rather than a single action type dropdown.
MOVE_TO_LABELS = {
    "delete": "Trash",
    "archive": "Archive",
    "spam": "Spam",
    "inbox": "Inbox - Default",
}

logger = logging.getLogger(__name__)


class ProtonMailSync:
    """Handles sync operations: upload Sieve, toggle filters, delete filters."""

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
        logger.info("Sync browser initialized")

    async def login(self) -> bool:
        """Login to ProtonMail (same logic as scraper)."""
        page = self.page
        await page.goto(PROTONMAIL_LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

        if self.credentials:
            try:
                await page.wait_for_selector(selectors.USERNAME_INPUT, timeout=ELEMENT_TIMEOUT_MS)
                await page.fill(selectors.USERNAME_INPUT, self.credentials.username)
                await page.click(selectors.LOGIN_BUTTON)
                await page.wait_for_selector(selectors.PASSWORD_INPUT, timeout=ELEMENT_TIMEOUT_MS)
                await page.fill(selectors.PASSWORD_INPUT, self.credentials.password)
                await page.click(selectors.LOGIN_BUTTON)
                await page.wait_for_url(
                    lambda url: "/mail/" in url or "/apps" in url,
                    timeout=LOGIN_TIMEOUT_MS
                )
                logger.info("Automated login successful")
                return True
            except Exception as e:
                raise RuntimeError(f"Automated login failed: {e}")
        else:
            print("\n>>> Please log in to ProtonMail in the browser window. <<<\n")
            await page.wait_for_url(
                lambda url: "/mail/" in url or "/apps" in url,
                timeout=LOGIN_TIMEOUT_MS
            )
            return True

    async def navigate_to_filters(self):
        """Navigate to filter settings page via the UI.

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

    async def upload_sieve(self, sieve_script: str) -> bool:
        """Upload a Sieve script to ProtonMail.

        Navigates to the Sieve editor tab and pastes the script.
        """
        page = self.page

        try:
            # Navigate to filters page first
            await self.navigate_to_filters()

            # Click on Sieve editor tab
            sieve_tab = await page.query_selector('[data-testid*="sieve"], button:has-text("Sieve"), [href*="sieve"]')
            if sieve_tab:
                await sieve_tab.click()
                await page.wait_for_timeout(2000)

            # Find the editor (CodeMirror)
            editor = await page.query_selector('.cm-content, .CodeMirror, textarea[class*="sieve"], textarea')
            if editor:
                # Clear existing content and paste new
                await editor.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await page.wait_for_timeout(500)

                # Type the sieve script
                await editor.fill(sieve_script) if await editor.get_attribute("contenteditable") != "true" else None
                if await editor.get_attribute("contenteditable") == "true":
                    await page.keyboard.type(sieve_script, delay=1)

                await page.wait_for_timeout(1000)

            # Click save
            save_btn = await page.query_selector('[data-testid*="save"], button:has-text("Save")')
            if save_btn:
                await save_btn.click()
                await page.wait_for_timeout(3000)
                logger.info("Sieve script uploaded successfully")
                return True

            logger.warning("Could not find save button for Sieve editor")
            return False

        except Exception as e:
            logger.error("Failed to upload Sieve script: %s", e)
            raise

    async def create_filter(
        self,
        name: str,
        conditions: List[Dict[str, str]],
        actions: List[Dict[str, str]],
        logic: str = "and",
    ) -> bool:
        """Create a new filter in ProtonMail via the multi-step wizard.

        The wizard has 4 steps: Name → Conditions → Actions → (Save on Actions).

        Args:
            name: Filter name
            conditions: List of dicts with keys: type, comparator, value
                e.g. [{"type": "sender", "comparator": "contains", "value": "spam@example.com"}]
            actions: List of dicts with keys: type, value (optional)
                Supported types: "delete" (move to Trash), "mark_read", "star",
                "archive", "move_to" (requires value like "Spam"/"Inbox")
            logic: "and" or "or" for condition matching

        Returns:
            True if filter was created successfully.
        """
        page = self.page

        try:
            # === Step 1: Name ===
            await page.click(selectors.ADD_FILTER_BUTTON)
            await page.wait_for_timeout(1500)

            await page.fill(selectors.FILTER_MODAL_NAME, name)
            await page.wait_for_timeout(300)

            # Click Next to go to Conditions
            await page.click(selectors.FILTER_MODAL_NEXT)
            await page.wait_for_timeout(1500)

            # === Step 2: Conditions ===

            # Set logic type (ALL vs ANY)
            if logic.lower() == "or":
                any_radio = await page.query_selector('text=ANY')
                if any_radio:
                    await any_radio.click()
                    await page.wait_for_timeout(300)

            for i, cond in enumerate(conditions):
                # Get the condition row
                cond_selector = selectors.FILTER_CONDITION_ROW_N.format(i)
                cond_row = await page.query_selector(cond_selector)
                if not cond_row:
                    logger.warning("Condition row %d not found", i)
                    continue

                # Get the two select buttons (type, comparator) in this row
                select_btns = await cond_row.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)

                # Set condition type
                cond_type = cond.get("type", "sender")
                type_label = CONDITION_TYPE_LABELS.get(cond_type, cond_type)
                if len(select_btns) >= 1:
                    await select_btns[0].click()
                    await page.wait_for_timeout(500)
                    opt = await page.query_selector(
                        f'{selectors.DROPDOWN_ITEM}:has-text("{type_label}")'
                    )
                    if opt:
                        await opt.click()
                        await page.wait_for_timeout(500)

                # Set comparator
                comparator = cond.get("comparator", "contains")
                comp_label = COMPARATOR_LABELS.get(comparator, comparator)
                # Re-query select buttons (DOM may have updated)
                select_btns = await cond_row.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
                if len(select_btns) >= 2:
                    current_label = await select_btns[1].get_attribute("aria-label")
                    if current_label != comp_label:
                        await select_btns[1].click()
                        await page.wait_for_timeout(500)
                        opt = await page.query_selector(
                            f'{selectors.DROPDOWN_ITEM}:has-text("{comp_label}")'
                        )
                        if opt:
                            await opt.click()
                            await page.wait_for_timeout(500)

                # Fill condition value
                value = cond.get("value", "")
                if value:
                    value_input = await cond_row.query_selector('input[type="text"]')
                    if value_input:
                        await value_input.fill(value)
                        await page.wait_for_timeout(300)
                        # Click "Insert" to commit the value
                        insert_btn = await cond_row.query_selector('button:has-text("Insert")')
                        if insert_btn:
                            await insert_btn.click()
                            await page.wait_for_timeout(500)

            # Click Next to go to Actions
            await page.click(selectors.FILTER_MODAL_NEXT)
            await page.wait_for_timeout(1500)

            # === Step 3: Actions ===
            for action in actions:
                action_type = action.get("type", "")

                if action_type in ("delete", "archive", "move_to", "spam"):
                    # Use the "Move to" folder dropdown
                    folder_name = MOVE_TO_LABELS.get(action_type)
                    if action_type == "move_to":
                        folder_name = action.get("value", "Inbox - Default")

                    if folder_name:
                        folder_select = await page.query_selector(
                            'button.select[aria-label="Do not move"], '
                            'button.select[aria-label*="move"], '
                            f'button.select[aria-label="{folder_name}"]'
                        )
                        if folder_select:
                            await folder_select.click()
                            await page.wait_for_timeout(500)
                            opt = await page.query_selector(
                                f'{selectors.DROPDOWN_ITEM}:has-text("{folder_name}")'
                            )
                            if opt:
                                await opt.click()
                                await page.wait_for_timeout(500)

                elif action_type == "mark_read":
                    # Check the "Read" checkbox in the Mark as section
                    mark_row = await page.query_selector(selectors.FILTER_ACTION_MARK_AS_ROW)
                    if mark_row:
                        read_label = await mark_row.query_selector('label:has-text("Read")')
                        if read_label:
                            checkbox = await read_label.query_selector('input[type="checkbox"]')
                            if checkbox and not await checkbox.is_checked():
                                await read_label.click()
                                await page.wait_for_timeout(300)
                    else:
                        # Fallback: find Read checkbox anywhere in the dialog
                        read_cb = await page.query_selector('label:has-text("Read") input[type="checkbox"]')
                        if read_cb and not await read_cb.is_checked():
                            label = await page.query_selector('label:has-text("Read")')
                            await label.click()
                            await page.wait_for_timeout(300)

                elif action_type == "star":
                    # Check the "Starred" checkbox
                    mark_row = await page.query_selector(selectors.FILTER_ACTION_MARK_AS_ROW)
                    if mark_row:
                        star_label = await mark_row.query_selector('label:has-text("Starred")')
                        if star_label:
                            checkbox = await star_label.query_selector('input[type="checkbox"]')
                            if checkbox and not await checkbox.is_checked():
                                await star_label.click()
                                await page.wait_for_timeout(300)

            # Click Save
            save_btn = await page.query_selector('button:has-text("Save")')
            if save_btn:
                await save_btn.click()
                await page.wait_for_timeout(3000)

            # Verify creation by checking page contains filter name
            page_text = await page.inner_text("body")
            if name in page_text:
                logger.info("Created filter: %s", name)
                return True

            logger.warning("Uncertain if filter '%s' was created", name)
            return True

        except Exception as e:
            logger.error("Failed to create filter '%s': %s", name, e)
            # Try to close any open modal
            try:
                close_btn = await page.query_selector(selectors.FILTER_MODAL_CLOSE)
                if close_btn:
                    await close_btn.click()
            except Exception:
                pass
            raise

    async def delete_all_filters(self) -> int:
        """Delete all filters on the page. Returns count deleted."""
        page = self.page
        deleted = 0

        while True:
            # Look for the actions dropdown button next to any filter
            dropdown = await page.query_selector(selectors.FILTER_ACTIONS_DROPDOWN)
            if not dropdown:
                break

            await dropdown.click()
            await page.wait_for_timeout(500)

            # Click "Delete" in the dropdown
            delete_item = await page.query_selector(
                f'{selectors.DROPDOWN_ITEM}:has-text("Delete")'
            )
            if delete_item:
                await delete_item.click()
                await page.wait_for_timeout(500)
            else:
                logger.warning("No Delete option in dropdown")
                break

            # Confirm deletion in the prompt dialog
            await page.wait_for_timeout(500)
            confirm_btn = await page.query_selector(selectors.DELETE_CONFIRM_BUTTON)
            if not confirm_btn:
                # Fallback: find the last visible Delete button on page
                all_btns = await page.query_selector_all('button:has-text("Delete")')
                for btn in reversed(all_btns):
                    if await btn.is_visible():
                        confirm_btn = btn
                        break
            if confirm_btn:
                await confirm_btn.click()
                await page.wait_for_timeout(2000)
                deleted += 1
                logger.info("Deleted a filter (%d so far)", deleted)
            else:
                logger.warning("Could not find delete confirmation button")
                break

        logger.info("Deleted %d filters total", deleted)
        return deleted

    async def close(self):
        """Close the browser."""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Sync browser closed")
