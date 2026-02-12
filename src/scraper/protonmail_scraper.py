"""Playwright automation for scraping ProtonMail filters."""

import logging
from typing import List, Optional

from src.scraper import selectors
from src.scraper.browser import ProtonMailBrowser, MODAL_TRANSITION_MS, DROPDOWN_MS

logger = logging.getLogger(__name__)

# Maps ProtonMail UI labels back to our model values
UI_TYPE_TO_MODEL = {
    "the sender": "sender",
    "the recipient": "recipient",
    "the subject": "subject",
    "the attachment": "attachments",
}

UI_OPERATOR_TO_MODEL = {
    "is exactly": "is",
    "begins with": "starts_with",
    "ends with": "ends_with",
}


class ProtonMailScraper(ProtonMailBrowser):
    """Scrapes filters from ProtonMail settings UI (read-only)."""

    async def scrape_all_filters(self) -> List[dict]:
        """Scrape all filters from the Custom filters section only.

        Returns list of dicts with filter data. Each dict has:
        - name: str
        - enabled: bool
        - conditions: list of dicts
        - actions: list of dicts

        Raises RuntimeError if the expected page structure is not found.
        """
        page = self.page
        filters = []

        # --- Layout assertions: fail loudly if structure changed ---
        await self._assert_filter_page_structure()

        # Scope to the Custom filters section only (not Spam/Allow lists)
        section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
        if not section:
            raise RuntimeError(
                "Could not find Custom filters section. "
                "Expected <section> containing h2 'Custom filters'. "
                "ProtonMail may have changed their UI layout."
            )

        filter_items = await section.query_selector_all(selectors.FILTER_TABLE_ROWS)
        total = len(filter_items)
        logger.info("Found %d filter items in Custom filters section", total)

        for idx, item in enumerate(filter_items):
            try:
                filter_data = await self._scrape_single_filter(item, idx)
                if filter_data:
                    filters.append(filter_data)
                    logger.info("Scraped filter %d/%d: %s", idx + 1, total, filter_data.get("name", "Unknown"))
            except Exception as e:
                logger.warning("Failed to scrape filter %d: %s", idx, e)

        return filters

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

    async def _scrape_single_filter(self, item, idx: int) -> Optional[dict]:
        """Scrape a single filter item from the list."""
        page = self.page

        # Get filter name from Edit button's aria-label
        name = ""
        edit_btn = await item.query_selector(selectors.FILTER_EDIT_BUTTON)
        if edit_btn:
            aria = await edit_btn.get_attribute("aria-label")
            if aria and '"' in aria:
                name = aria.split('"')[1]
        if not name:
            tds = await item.query_selector_all("td")
            if len(tds) >= 2:
                name = (await tds[1].inner_text()).strip()
            elif tds:
                name = (await tds[0].inner_text()).strip()
        if not name:
            name_el = await item.query_selector(selectors.FILTER_NAME_FALLBACK)
            name = await name_el.inner_text() if name_el else f"Filter {idx}"
            name = name.strip()

        # Get enabled state from toggle
        toggle_input = await item.query_selector(selectors.FILTER_TOGGLE)
        enabled = True
        if toggle_input:
            enabled = await toggle_input.is_checked()

        # Open edit wizard to get conditions/actions
        conditions = []
        actions = []
        logic = "and"

        try:
            edit_btn = await item.query_selector(
                f'{selectors.FILTER_EDIT_BUTTON}, {selectors.FILTER_EDIT_BUTTON_ALT}'
            )
            if edit_btn:
                await edit_btn.click()
                await page.wait_for_timeout(MODAL_TRANSITION_MS)

                # Wizard opens on Name step - click Next to go to Conditions
                next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
                if next_btn:
                    await next_btn.click()
                    await page.wait_for_timeout(MODAL_TRANSITION_MS)

                    conditions = await self._scrape_conditions()
                    logic = await self._scrape_logic()

                    # Click Next to go to Actions step
                    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
                    if next_btn:
                        await next_btn.click()
                        await page.wait_for_timeout(MODAL_TRANSITION_MS)
                        actions = await self._scrape_actions()

                # Close modal
                close_btn = await page.query_selector(
                    f'{selectors.FILTER_MODAL_CLOSE}, {selectors.CANCEL_BUTTON}'
                )
                if close_btn:
                    await close_btn.click()
                    await page.wait_for_timeout(DROPDOWN_MS)
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

        condition_rows = await page.query_selector_all(selectors.FILTER_CONDITION_ROWS)

        for row in condition_rows:
            try:
                select_btns = await row.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
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

                cond_type = UI_TYPE_TO_MODEL.get(cond_type, cond_type)
                operator = UI_OPERATOR_TO_MODEL.get(operator, operator)

                # Get values - check for tags/chips first, then input
                value = ""
                tags = await row.query_selector_all(selectors.CONDITION_VALUE_TAGS)
                if tags:
                    tag_texts = []
                    for tag in tags:
                        tag_texts.append((await tag.inner_text()).strip())
                    value = ", ".join(tag_texts)
                else:
                    value_el = await row.query_selector(selectors.CONDITION_VALUE_INPUT)
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
        """Scrape actions from the Actions step of the filter wizard."""
        page = self.page
        actions = []

        # Check "Move to" folder selection
        folder_row = await page.query_selector(selectors.FILTER_ACTION_FOLDER_ROW)
        if folder_row:
            folder_btn = await folder_row.query_selector(selectors.CUSTOM_SELECT_BUTTON)
            if folder_btn:
                label = await folder_btn.get_attribute("aria-label")
                # Strip hierarchy bullet prefix (e.g. " • myjunk" -> "myjunk")
                if label:
                    label = label.lstrip(" \t•·").strip()
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
            read_cb = await mark_row.query_selector(selectors.MARK_READ_CHECKBOX)
            if read_cb and await read_cb.is_checked():
                actions.append({"type": "mark_read", "parameters": {}})
            star_cb = await mark_row.query_selector(selectors.MARK_STARRED_CHECKBOX)
            if star_cb and await star_cb.is_checked():
                actions.append({"type": "star", "parameters": {}})

        return actions

    async def _scrape_logic(self) -> str:
        """Scrape the logic type (AND/OR) from the Conditions step."""
        page = self.page
        try:
            any_radio = await page.query_selector('input[type="radio"]:checked')
            if any_radio:
                label = await any_radio.evaluate(
                    'el => el.closest("label")?.textContent || ""'
                )
                if "any" in label.lower():
                    return "or"
        except Exception:
            pass
        return "and"
