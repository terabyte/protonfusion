"""Playwright automation for sync/restore operations on ProtonMail."""

import logging
from typing import Dict, List, Optional

from src.scraper import selectors
from src.scraper.browser import (
    ProtonMailBrowser, MODAL_TRANSITION_MS, DROPDOWN_MS,
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

# Maps our model action types to ProtonMail "Move to" folder labels
MOVE_TO_LABELS = {
    "delete": "Trash",
    "archive": "Archive",
    "spam": "Spam",
    "inbox": "Inbox - Default",
}

logger = logging.getLogger(__name__)


class ProtonMailSync(ProtonMailBrowser):
    """Handles sync operations: create/delete/toggle filters, upload Sieve."""

    async def upload_sieve(self, sieve_script: str) -> bool:
        """Upload a Sieve script to ProtonMail's Sieve editor."""
        page = self.page

        try:
            # Click on Sieve editor tab
            sieve_tab = await page.query_selector(selectors.SIEVE_TAB)
            if sieve_tab:
                await sieve_tab.click()
                await page.wait_for_timeout(MODAL_TRANSITION_MS)

            # Find the editor
            editor = await page.query_selector(selectors.SIEVE_EDITOR)
            if editor:
                await editor.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await page.wait_for_timeout(DROPDOWN_MS)

                if await editor.get_attribute("contenteditable") != "true":
                    await editor.fill(sieve_script)
                else:
                    await page.keyboard.type(sieve_script, delay=1)

                await page.wait_for_timeout(DROPDOWN_MS)

            # Click save
            save_btn = await page.query_selector(selectors.SIEVE_SAVE_BUTTON)
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
        """Create a new filter via the multi-step wizard.

        Args:
            name: Filter name
            conditions: List of dicts with keys: type, comparator, value
            actions: List of dicts with keys: type, value (optional)
            logic: "and" or "or" for condition matching

        Returns:
            True if filter was created successfully.
        """
        page = self.page

        try:
            # Step 1: Name
            await page.click(selectors.ADD_FILTER_BUTTON)
            await page.wait_for_timeout(MODAL_TRANSITION_MS)

            await page.fill(selectors.FILTER_MODAL_NAME, name)
            await page.wait_for_timeout(300)

            await page.click(selectors.FILTER_MODAL_NEXT)
            await page.wait_for_timeout(MODAL_TRANSITION_MS)

            # Step 2: Conditions
            if logic.lower() == "or":
                any_radio = await page.query_selector('text=ANY')
                if any_radio:
                    await any_radio.click()
                    await page.wait_for_timeout(300)

            for i, cond in enumerate(conditions):
                cond_selector = selectors.FILTER_CONDITION_ROW_N.format(i)
                cond_row = await page.query_selector(cond_selector)
                if not cond_row:
                    logger.warning("Condition row %d not found", i)
                    continue

                select_btns = await cond_row.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)

                # Set condition type
                cond_type = cond.get("type", "sender")
                type_label = CONDITION_TYPE_LABELS.get(cond_type, cond_type)
                if len(select_btns) >= 1:
                    await select_btns[0].click()
                    await page.wait_for_timeout(DROPDOWN_MS)
                    opt = await page.query_selector(
                        f'{selectors.DROPDOWN_ITEM}:has-text("{type_label}")'
                    )
                    if opt:
                        await opt.click()
                        await page.wait_for_timeout(DROPDOWN_MS)

                # Set comparator
                comparator = cond.get("comparator", "contains")
                comp_label = COMPARATOR_LABELS.get(comparator, comparator)
                select_btns = await cond_row.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
                if len(select_btns) >= 2:
                    current_label = await select_btns[1].get_attribute("aria-label")
                    if current_label != comp_label:
                        await select_btns[1].click()
                        await page.wait_for_timeout(DROPDOWN_MS)
                        opt = await page.query_selector(
                            f'{selectors.DROPDOWN_ITEM}:has-text("{comp_label}")'
                        )
                        if opt:
                            await opt.click()
                            await page.wait_for_timeout(DROPDOWN_MS)

                # Fill condition value
                value = cond.get("value", "")
                if value:
                    value_input = await cond_row.query_selector(selectors.CONDITION_VALUE_INPUT)
                    if value_input:
                        await value_input.fill(value)
                        await page.wait_for_timeout(300)
                        insert_btn = await cond_row.query_selector(selectors.CONDITION_INSERT_BUTTON)
                        if insert_btn:
                            await insert_btn.click()
                            await page.wait_for_timeout(DROPDOWN_MS)

            # Go to Actions step
            await page.click(selectors.FILTER_MODAL_NEXT)
            await page.wait_for_timeout(MODAL_TRANSITION_MS)

            # Step 3: Actions
            for action in actions:
                action_type = action.get("type", "")

                if action_type in ("delete", "archive", "move_to", "spam"):
                    folder_name = MOVE_TO_LABELS.get(action_type)
                    if action_type == "move_to":
                        folder_name = action.get("value", "Inbox - Default")

                    if folder_name:
                        folder_select = await page.query_selector(
                            f'{selectors.FOLDER_SELECT}, '
                            f'button.select[aria-label="{folder_name}"]'
                        )
                        if folder_select:
                            await folder_select.click()
                            await page.wait_for_timeout(DROPDOWN_MS)
                            opt = await page.query_selector(
                                f'{selectors.DROPDOWN_ITEM}:has-text("{folder_name}")'
                            )
                            if opt:
                                await opt.click()
                                await page.wait_for_timeout(DROPDOWN_MS)

                elif action_type == "mark_read":
                    await self._toggle_mark_checkbox(selectors.MARK_READ_LABEL, selectors.MARK_READ_CHECKBOX)

                elif action_type == "star":
                    await self._toggle_mark_checkbox(selectors.MARK_STARRED_LABEL, selectors.MARK_STARRED_CHECKBOX)

            # Save
            save_btn = await page.query_selector(selectors.SAVE_BUTTON)
            if save_btn:
                await save_btn.click()
                await page.wait_for_timeout(3000)

            # Verify
            page_text = await page.inner_text("body")
            if name in page_text:
                logger.info("Created filter: %s", name)
                return True

            logger.warning("Uncertain if filter '%s' was created", name)
            return True

        except Exception as e:
            logger.error("Failed to create filter '%s': %s", name, e)
            try:
                close_btn = await page.query_selector(selectors.FILTER_MODAL_CLOSE)
                if close_btn:
                    await close_btn.click()
            except Exception:
                pass
            raise

    async def _toggle_mark_checkbox(self, label_selector: str, checkbox_selector: str):
        """Check a "Mark as" checkbox if not already checked."""
        page = self.page
        mark_row = await page.query_selector(selectors.FILTER_ACTION_MARK_AS_ROW)
        if mark_row:
            label = await mark_row.query_selector(label_selector)
            if label:
                checkbox = await label.query_selector('input[type="checkbox"]')
                if checkbox and not await checkbox.is_checked():
                    await label.click()
                    await page.wait_for_timeout(300)
                    return
        # Fallback: find anywhere in dialog
        checkbox = await page.query_selector(checkbox_selector)
        if checkbox and not await checkbox.is_checked():
            label = await page.query_selector(label_selector)
            if label:
                await label.click()
                await page.wait_for_timeout(300)

    async def enable_filter(self, name: str) -> bool:
        """Enable a filter by name by clicking its toggle."""
        return await self._set_filter_toggle(name, enabled=True)

    async def disable_filter(self, name: str) -> bool:
        """Disable a filter by name by clicking its toggle."""
        return await self._set_filter_toggle(name, enabled=False)

    async def _set_filter_toggle(self, name: str, enabled: bool) -> bool:
        """Set a filter's toggle state by finding the row with the given name."""
        page = self.page
        rows = await page.query_selector_all(selectors.FILTER_TABLE_ROWS)

        for row in rows:
            row_name = await self._get_filter_name(row)
            if row_name == name:
                toggle = await row.query_selector(selectors.FILTER_TOGGLE)
                if toggle:
                    is_checked = await toggle.is_checked()
                    if is_checked != enabled:
                        await toggle.click()
                        await page.wait_for_timeout(1000)
                        logger.info("%s filter: %s", "Enabled" if enabled else "Disabled", name)
                    else:
                        logger.info("Filter '%s' already %s", name, "enabled" if enabled else "disabled")
                    return True

        logger.warning("Filter '%s' not found", name)
        return False

    async def disable_all_ui_filters(self) -> int:
        """Disable all enabled filters. Returns count disabled."""
        page = self.page
        disabled = 0
        rows = await page.query_selector_all(selectors.FILTER_TABLE_ROWS)

        for row in rows:
            toggle = await row.query_selector(selectors.FILTER_TOGGLE)
            if toggle and await toggle.is_checked():
                await toggle.click()
                await page.wait_for_timeout(1000)
                disabled += 1

        logger.info("Disabled %d filters", disabled)
        return disabled

    async def delete_filter(self, name: str) -> bool:
        """Delete a single filter by name."""
        page = self.page
        rows = await page.query_selector_all(selectors.FILTER_TABLE_ROWS)

        for row in rows:
            row_name = await self._get_filter_name(row)
            if row_name == name:
                dropdown = await row.query_selector(selectors.FILTER_ACTIONS_DROPDOWN)
                if not dropdown:
                    continue

                await dropdown.click()
                await page.wait_for_timeout(DROPDOWN_MS)

                delete_item = await page.query_selector(
                    f'{selectors.DROPDOWN_ITEM}:has-text("Delete")'
                )
                if delete_item:
                    await delete_item.click()
                    await page.wait_for_timeout(DROPDOWN_MS)

                    await self._confirm_delete()
                    logger.info("Deleted filter: %s", name)
                    return True

        logger.warning("Filter '%s' not found for deletion", name)
        return False

    async def delete_all_filters(self) -> int:
        """Delete all filters on the page. Returns count deleted."""
        page = self.page
        deleted = 0

        while True:
            dropdown = await page.query_selector(selectors.FILTER_ACTIONS_DROPDOWN)
            if not dropdown:
                break

            await dropdown.click()
            await page.wait_for_timeout(DROPDOWN_MS)

            delete_item = await page.query_selector(
                f'{selectors.DROPDOWN_ITEM}:has-text("Delete")'
            )
            if delete_item:
                await delete_item.click()
                await page.wait_for_timeout(DROPDOWN_MS)
            else:
                logger.warning("No Delete option in dropdown")
                break

            if await self._confirm_delete():
                deleted += 1
                logger.info("Deleted a filter (%d so far)", deleted)
            else:
                break

        logger.info("Deleted %d filters total", deleted)
        return deleted

    async def _confirm_delete(self) -> bool:
        """Confirm a delete dialog."""
        page = self.page
        await page.wait_for_timeout(DROPDOWN_MS)
        confirm_btn = await page.query_selector(selectors.DELETE_CONFIRM_BUTTON)
        if not confirm_btn:
            # Fallback: last visible Delete button
            all_btns = await page.query_selector_all('button:has-text("Delete")')
            for btn in reversed(all_btns):
                if await btn.is_visible():
                    confirm_btn = btn
                    break
        if confirm_btn:
            await confirm_btn.click()
            await page.wait_for_timeout(2000)
            return True
        logger.warning("Could not find delete confirmation button")
        return False

    async def _get_filter_name(self, row) -> str:
        """Extract filter name from a table row."""
        edit_btn = await row.query_selector(selectors.FILTER_EDIT_BUTTON)
        if edit_btn:
            aria = await edit_btn.get_attribute("aria-label")
            if aria and '"' in aria:
                return aria.split('"')[1]
        tds = await row.query_selector_all("td")
        if len(tds) >= 2:
            return (await tds[1].inner_text()).strip()
        elif tds:
            return (await tds[0].inner_text()).strip()
        return ""
