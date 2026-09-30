"""Playwright automation for scraping ProtonMail filters."""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Page

from src.scraper import selectors
from src.scraper.browser import (
    ProtonMailBrowser, MODAL_TRANSITION_MS, DROPDOWN_MS, FILTERS_PAGE_LOAD_MS,
)
from src.utils.config import FILTERS_DIRECT_URL, PAGE_LOAD_TIMEOUT_MS

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


# Folder names that are special actions, not real folder targets
SPECIAL_FOLDERS = {"Do not move", "Inbox - Default", "Trash", "Archive", "Spam"}

# Bullet characters used by ProtonMail to indicate subfolder nesting
BULLET_CHARS = " \t•·"

# Label-row text that means "no label selected" rather than naming a label.
LABEL_PLACEHOLDERS = {
    "", "do not label", "no label", "no labels", "none",
    "choose label", "choose labels", "choose label(s)",
    "select label", "select labels", "select label(s)", "add label",
}

# Label-row text that is UI chrome (headings), not a label name.
LABEL_ROW_CHROME = LABEL_PLACEHOLDERS | {"label as", "label", "labels", "apply label", "apply labels"}

# Glyphs a chip may render for its remove button, stripped from chip text.
CHIP_REMOVE_GLYPHS = "×✕✖"

# A dropdown button summarising the selection as a count ("2 labels")
# instead of naming the labels; the names cannot be recovered from it.
LABEL_COUNT_PATTERN = re.compile(r"^\d+\s+labels?\b", re.IGNORECASE)


def _parse_label_row(
    chip_texts: List[str], button_label: str, button_text: str, row_text: str,
) -> Tuple[List[str], Optional[str]]:
    """Turn what the "Label as" row shows into label names.

    Pure function so the rules can be unit-tested without a browser; the
    DOM reading lives in ProtonMailScraper._read_label_row.

    Chips win when present (one chip per selected label, so a label name
    containing a comma survives). Otherwise the dropdown button's
    aria-label (then its text) is split on commas. Placeholders such as
    "Do not label" mean no label.

    Returns (labels, issue). issue is None when the row was fully
    accounted for. It is set when the row cannot be read reliably: the
    button shows a count instead of names, or the row contains text that
    is neither a parsed label nor known chrome. That last check is the
    guard against a UI shape this reader does not know about silently
    reading as "no labels".
    """
    labels: List[str] = []

    def _add(name: str):
        name = name.strip().strip(CHIP_REMOVE_GLYPHS).strip()
        if name.lower() in LABEL_PLACEHOLDERS or name in labels:
            return
        labels.append(name)

    if chip_texts:
        for text in chip_texts:
            _add(text)
    else:
        summary = (button_label or button_text or "").strip()
        if LABEL_COUNT_PATTERN.match(summary):
            return [], f"label row shows a count ({summary!r}), not label names"
        for part in summary.split(","):
            _add(part)

    # Every piece of visible text must be a label we parsed or known chrome.
    unexplained = []
    for line in row_text.splitlines():
        line = line.strip().strip(CHIP_REMOVE_GLYPHS).strip()
        if not line:
            continue
        if line in labels:
            continue
        pieces = [piece.strip() for piece in line.split(",")]
        if all(piece in labels or piece.lower() in LABEL_ROW_CHROME for piece in pieces):
            continue
        unexplained.append(line)
    if unexplained:
        return labels, f"label row has text the reader did not account for: {unexplained!r}"

    return labels, None


def _distribute_indices(total: int, workers: int) -> List[List[int]]:
    """Split filter indices into contiguous chunks across workers.

    Given total=10 and workers=3, returns [[0,1,2,3], [4,5,6], [7,8,9]].
    """
    if total <= 0 or workers <= 0:
        return []
    workers = min(workers, total)
    base_size = total // workers
    remainder = total % workers
    chunks = []
    start = 0
    for i in range(workers):
        size = base_size + (1 if i < remainder else 0)
        chunks.append(list(range(start, start + size)))
        start += size
    return chunks


class ProtonMailScraper(ProtonMailBrowser):
    """Scrapes filters from ProtonMail settings UI (read-only)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._folder_path_map: Optional[dict] = None

    async def scrape_all_filters(self, workers: int = 1) -> List[dict]:
        """Scrape all filters from the Custom filters section only.

        Args:
            workers: Number of parallel browser tabs to use (1=sequential).

        Returns list of dicts with filter data. Each dict has:
        - name: str
        - enabled: bool
        - conditions: list of dicts
        - actions: list of dicts

        Raises RuntimeError if the expected page structure is not found.
        """
        page = self.page

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

        if workers <= 1 or total <= 1:
            return await self._scrape_all_sequential(filter_items, total)

        # Parallel path - workers navigate to the same filters page
        self._filters_page_url = page.url or FILTERS_DIRECT_URL
        workers = min(workers, total)
        chunks = _distribute_indices(total, workers)
        logger.info("Scraping with %d parallel workers", workers)

        worker_results = await asyncio.gather(
            *[self._scrape_worker(wid, chunk) for wid, chunk in enumerate(chunks)],
            return_exceptions=True,
        )

        # Merge results in priority order
        merged: Dict[int, dict] = {}
        for result in worker_results:
            if isinstance(result, Exception):
                logger.warning("Worker failed: %s", result)
            elif isinstance(result, dict):
                merged.update(result)

        filters = [merged[idx] for idx in sorted(merged.keys())]
        logger.info("Parallel scraping complete: %d filters collected", len(filters))
        return filters

    async def _scrape_all_sequential(self, filter_items, total: int) -> List[dict]:
        """Scrape all filters sequentially using the main page."""
        filters = []
        for idx, item in enumerate(filter_items):
            try:
                filter_data = await self._scrape_single_filter(item, idx)
                if filter_data:
                    filters.append(filter_data)
                    logger.info("Scraped filter %d/%d: %s", idx + 1, total, filter_data.get("name", "Unknown"))
            except Exception as e:
                logger.warning("Failed to scrape filter %d: %s", idx, e)
        return filters

    async def _scrape_worker(self, worker_id: int, indices: List[int]) -> Dict[int, dict]:
        """Scrape assigned filter indices using a dedicated browser tab."""
        page = await self.create_worker_page()
        try:
            url = getattr(self, '_filters_page_url', FILTERS_DIRECT_URL)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PAGE_LOAD_TIMEOUT_MS,
            )
            await page.wait_for_timeout(FILTERS_PAGE_LOAD_MS)

            section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
            if not section:
                logger.warning("Worker %d: Custom filters section not found", worker_id)
                return {}

            items = await section.query_selector_all(selectors.FILTER_TABLE_ROWS)
            results: Dict[int, dict] = {}

            for idx in indices:
                if idx < len(items):
                    try:
                        data = await self._scrape_single_filter(items[idx], idx, page=page)
                        if data:
                            results[idx] = data
                            logger.info(
                                "Worker %d: scraped filter %d: %s",
                                worker_id, idx, data.get("name", "Unknown"),
                            )
                    except Exception as e:
                        logger.warning("Worker %d: failed to scrape filter %d: %s", worker_id, idx, e)

            return results
        finally:
            await page.close()

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

    async def _scrape_single_filter(self, item, idx: int, page: Page = None) -> Optional[dict]:
        """Scrape a single filter item from the list."""
        if page is None:
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

                    conditions = await self._scrape_conditions(page=page)
                    logic = await self._scrape_logic(page=page)

                    # Click Next to go to Actions step
                    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
                    if next_btn:
                        await next_btn.click()
                        await page.wait_for_timeout(MODAL_TRANSITION_MS)
                        actions = await self._scrape_actions(page=page)

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

    async def _scrape_conditions(self, page: Page = None) -> List[dict]:
        """Scrape conditions from the Conditions step of the filter wizard."""
        if page is None:
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

    async def _scrape_actions(self, page: Page = None) -> List[dict]:
        """Scrape actions from the Actions step of the filter wizard."""
        if page is None:
            page = self.page
        actions = []

        # Check "Move to" folder selection
        folder_row = await page.query_selector(selectors.FILTER_ACTION_FOLDER_ROW)
        if folder_row:
            folder_btn = await folder_row.query_selector(selectors.CUSTOM_SELECT_BUTTON)
            if folder_btn:
                raw_label = await folder_btn.get_attribute("aria-label")
                if raw_label and raw_label.strip() != "Do not move":
                    # Build folder path map on first encounter
                    if self._folder_path_map is None:
                        await self._build_folder_path_map(folder_btn, page=page)

                    folder = self._resolve_folder_path(raw_label)
                    folder_map = {
                        "Trash": "delete",
                        "Archive": "archive",
                        "Spam": "move_to",
                        "Inbox - Default": "move_to",
                    }
                    action_type = folder_map.get(folder, "move_to")
                    if action_type in ("delete", "archive"):
                        actions.append({"type": action_type, "parameters": {}})
                    else:
                        actions.append({"type": "move_to", "parameters": {"folder": folder}})

        # Check "Label as" selection (a filter can apply several labels)
        label_row = await page.query_selector(selectors.FILTER_ACTION_LABEL_ROW)
        if label_row:
            labels, issue = await self._read_label_row(label_row)
            if issue:
                logger.warning("Label row: %s", issue)
            for label in labels:
                actions.append({"type": "label", "parameters": {"label": label}})

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

    async def _read_label_row(self, label_row) -> Tuple[List[str], Optional[str]]:
        """Read the selected labels from the Actions step's "Label as" row.

        The only DOM-reading code for labels; adjust here after a live check.
        UNVERIFIED against the live ProtonMail UI. Assumed shape:

            <div data-testid="filter-modal:label-row">
              ...optional heading text such as "Label as"...
              <!-- selected labels as chips, one per label: -->
              <li class="label-stack-item"><span class="label-stack-item-text">Work</span></li>
              <!-- and/or a dropdown button naming the selection: -->
              <button class="select" aria-label="Work, Personal">Work, Personal</button>
            </div>

        Chips are read from FILTER_LABEL_CHIPS (their title attribute, else
        their text). With no chips, the first button matched by
        FILTER_LABEL_BUTTONS is read (aria-label, else text) and split on
        commas. "Do not label" and similar placeholders mean no label.
        The row's full visible text is passed along so _parse_label_row can
        flag anything it could not account for.
        """
        chip_texts = []
        for chip in await label_row.query_selector_all(selectors.FILTER_LABEL_CHIPS):
            text = await chip.get_attribute("title") or await chip.inner_text()
            chip_texts.append(text)

        button = None
        button_label = ""
        button_text = ""
        for button_selector in selectors.FILTER_LABEL_BUTTONS:
            button = await label_row.query_selector(button_selector)
            if button:
                button_label = await button.get_attribute("aria-label") or ""
                button_text = await button.inner_text()
                break

        row_text = await label_row.inner_text()
        if not chip_texts and button is None:
            # The row must offer some way to pick labels; with neither chips
            # nor a button this is a layout the reader does not know.
            return [], "label row has no label chips and no dropdown button"
        return _parse_label_row(chip_texts, button_label, button_text, row_text)

    async def _build_folder_path_map(self, folder_btn, page: Page = None):
        """Build a map from dropdown display text to full folder path.

        Opens the folder dropdown, reads all items in order, and reconstructs
        the hierarchy from bullet prefixes.  ProtonMail prefixes each nesting
        level with one bullet character (``•`` or ``·``), so counting bullets
        gives the depth and a simple path stack reconstructs the full path.
        """
        if page is None:
            page = self.page
        self._folder_path_map = {}

        try:
            await folder_btn.click()
            await page.wait_for_timeout(DROPDOWN_MS)

            items = await page.query_selector_all(selectors.DROPDOWN_ITEM)
            # Stack of ancestor folder names; path_stack[0] is top-level,
            # path_stack[1] is depth-1 child, etc.
            path_stack: List[str] = []

            for item in items:
                text = (await item.inner_text()).strip()
                if not text:
                    continue

                clean = text.lstrip(BULLET_CHARS).strip()

                if clean in SPECIAL_FOLDERS:
                    continue

                # Determine nesting depth by counting bullet characters
                prefix = text[: len(text) - len(text.lstrip(BULLET_CHARS))]
                depth = sum(1 for ch in prefix if ch in "•·")

                # Trim the stack to the current depth and push this folder
                path_stack = path_stack[:depth]
                path_stack.append(clean)

                full_path = "/".join(path_stack)
                self._folder_path_map[text] = full_path
                self._folder_path_map[clean] = full_path

            # Close the dropdown by pressing Escape
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(DROPDOWN_MS)

            logger.info(
                "Built folder path map: %d entries (%d nested)",
                len(self._folder_path_map),
                sum(1 for v in self._folder_path_map.values() if "/" in v),
            )
        except Exception as e:
            logger.warning("Failed to build folder path map: %s", e)
            self._folder_path_map = {}

    def _resolve_folder_path(self, raw_label: str) -> str:
        """Resolve a raw aria-label to the full folder path."""
        if self._folder_path_map:
            # Try exact match first (includes bullet prefix)
            if raw_label in self._folder_path_map:
                return self._folder_path_map[raw_label]
            # Try stripped version
            clean = raw_label.lstrip(BULLET_CHARS).strip()
            if clean in self._folder_path_map:
                return self._folder_path_map[clean]
            return clean
        # No map available, fall back to stripping bullets
        return raw_label.lstrip(BULLET_CHARS).strip()

    async def _scrape_logic(self, page: Page = None) -> str:
        """Scrape the logic type (AND/OR) from the Conditions step."""
        if page is None:
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
