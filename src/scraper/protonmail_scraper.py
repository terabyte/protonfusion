"""Playwright automation for scraping ProtonMail filters."""

import asyncio
import copy
import logging
import re
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Page

from src.models.filter_models import SYSTEM_FOLDER_ACTIONS
from src.scraper import selectors
from src.scraper.browser import (
    ProtonMailBrowser, MODAL_TRANSITION_MS, DROPDOWN_MS, FILTERS_PAGE_LOAD_MS,
)
from src.utils.config import FILTERS_PATH, PAGE_LOAD_TIMEOUT_MS

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


# Dropdown entries that are not user folders: "Do not move" plus the system
# folders, which map to fixed actions (SYSTEM_FOLDER_ACTIONS).
SPECIAL_FOLDERS = {"Do not move", *SYSTEM_FOLDER_ACTIONS}

# Bullet characters used by ProtonMail to indicate subfolder nesting
BULLET_CHARS = " \t•·"

# Model values the scraper can produce for a condition; anything else read
# from the UI is reported as a scrape issue instead of guessed at.
KNOWN_CONDITION_TYPES = set(UI_TYPE_TO_MODEL.values())
KNOWN_OPERATORS = {"contains", "is", "matches", "starts_with", "ends_with"}

# Action rows _scrape_actions understands. Any other visible
# filter-modal:*-row in the Actions step marks the filter incomplete.
KNOWN_ACTION_ROW_SELECTORS = [
    selectors.FILTER_ACTION_FOLDER_ROW,
    selectors.FILTER_ACTION_LABEL_ROW,
    selectors.FILTER_ACTION_MARK_AS_ROW,
    selectors.FILTER_ACTION_AUTO_REPLY_ROW,
]
KNOWN_ACTION_ROW_TESTIDS = {
    "filter-modal:folder-row", "filter-modal:label-row",
    "filter-modal:mark-as-row", "filter-modal:auto-reply-row",
}

# A checkbox's live state. `checked` is a DOM property that the UI sets
# without touching the HTML attribute, so attribute reads always say "off".
CHECKED_JS = "el => el.checked"

# Mark-as checkboxes we model (mark_read, star), by their label text.
KNOWN_MARK_AS_LABELS = {"read", "starred"}

# Label text of a checkbox: its enclosing <label>, else its aria-label.
CHECKBOX_LABEL_JS = (
    'el => (el.closest("label")?.innerText || el.getAttribute("aria-label") || "").trim()'
)

# Raw evidence for one wizard step: the enclosing modal's visible text plus
# the state of its visible form fields, which innerText leaves out. "" when
# the anchor is not inside a modal: falling back to the page body would save
# the whole settings page, every visible input included, as evidence.
STEP_TEXT_JS = """
(anchor) => {
  const root = anchor.closest('dialog, [role="dialog"], [class*="modal"]');
  if (!root) return '';
  const shown = el => el.offsetParent !== null || el.getClientRects().length > 0;
  const lines = [root.innerText.trim()];
  for (const field of root.querySelectorAll('input, textarea, select')) {
    const wrapper = field.closest('label');
    // Styled checkboxes hide the input itself but show its <label>
    if (!shown(field) && !(wrapper && shown(wrapper))) continue;
    const name = ((wrapper && wrapper.innerText) || field.getAttribute('aria-label') || field.name || '').trim();
    if (field.type === 'checkbox' || field.type === 'radio') {
      lines.push(`[${field.type}] ${name}: ${field.checked ? 'checked' : 'unchecked'}`);
    } else {
      lines.push(`[${field.tagName.toLowerCase()}] ${name} = ${field.value}`);
    }
  }
  for (const button of root.querySelectorAll('button[aria-label]')) {
    if (shown(button)) lines.push(`[button] ${button.getAttribute('aria-label')}`);
  }
  return lines.join('\\n');
}
"""

# Label-row text that is UI chrome, not a label: the row's collapse toggle
# and its "Create label" button.
LABEL_ROW_CHROME = {"label as", "create label"}


def _parse_label_row(
    options: List[Tuple[str, bool]], row_text: str,
) -> Tuple[List[str], Optional[str]]:
    """Turn the "Label as" row's checkbox options into applied label names.

    Pure function so the rules can be unit-tested without a browser; the
    DOM reading lives in ProtonMailScraper._read_label_row.

    `options` is one (name, ticked) pair per label checkbox. The row lists
    EVERY label on the account, so only ticked ones are applied; unticked
    ones are the normal case and not an issue.

    Returns (labels, issue). issue is set when the row cannot be read
    reliably: an option with no readable name, or visible row text that is
    neither a label name nor known chrome. That last check is the guard
    against a layout change silently reading as "no labels".
    """
    labels: List[str] = []
    for name, ticked in options:
        name = name.strip()
        if not name:
            return labels, "label row has a checkbox with no readable name"
        if ticked and name not in labels:
            labels.append(name)

    # Every piece of visible text must be a label name or known chrome.
    # Inline elements run together in innerText, so known tokens are cut
    # out of each line rather than matched whole.
    names = {name.strip() for name, _ in options}
    known_tokens = sorted((names | LABEL_ROW_CHROME) - {""}, key=len, reverse=True)
    unexplained = []
    for line in row_text.splitlines():
        rest = line
        for token in known_tokens:
            rest = re.sub(re.escape(token), " ", rest, flags=re.IGNORECASE)
        if rest.strip(" ,;\t"):
            unexplained.append(line.strip())
    if unexplained:
        return labels, f"label row has text the reader did not account for: {unexplained!r}"

    return labels, None


def _unread_filter_stub(idx: int, reason: str) -> dict:
    """Placeholder for a filter row that could not be scraped at all.

    Keeps the row visible in the results (flagged incomplete) instead of
    silently dropping it, which would let a later cleanup treat the live
    filter as though it had never been backed up.
    """
    return {
        "name": f"Filter {idx} (unread)",
        "enabled": True,
        "priority": idx,
        "logic": "and",
        "conditions": [],
        "actions": [],
        "raw": None,
        "scrape_issues": [reason],
    }


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
        self._filters_page_url = page.url or self.account_url(FILTERS_PATH)
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

        # A row no worker returned must not vanish from the backup; it
        # stays in the list, flagged, so the caller sees it was not read.
        filters = [
            merged[idx] if idx in merged else _unread_filter_stub(idx, "no worker returned this filter")
            for idx in range(total)
        ]
        logger.info("Parallel scraping complete: %d filters collected", len(merged))
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
                filters.append(_unread_filter_stub(idx, f"scrape failed: {e}"))
        return filters

    async def _scrape_worker(self, worker_id: int, indices: List[int]) -> Dict[int, dict]:
        """Scrape assigned filter indices using a dedicated browser tab."""
        page = await self.create_worker_page()
        try:
            url = getattr(self, '_filters_page_url', None) or self.account_url(FILTERS_PATH)
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

    async def _scrape_single_filter(self, item, idx: int, page: Page = None) -> Optional[dict]:
        """Scrape a single filter item from the list.

        Anything that stops the wizard being read in full (no Edit button,
        wizard not opening, a row the scraper cannot parse, an exception) is
        recorded in the returned "scrape_issues" list instead of being
        swallowed, and the wizard's visible text is kept in "raw".
        """
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
        issues: List[str] = []
        raw = {"conditions_text": "", "actions_text": "", "sieve_text": ""}
        is_sieve = False

        try:
            edit_btn = await item.query_selector(
                f'{selectors.FILTER_EDIT_BUTTON}, {selectors.FILTER_EDIT_BUTTON_ALT}'
            )
            if not edit_btn:
                issues.append("no Edit button in the filter row; wizard not opened")
            else:
                await edit_btn.click()
                await page.wait_for_timeout(MODAL_TRANSITION_MS)

                sieve_text = await self._read_sieve_editor(page)
                if sieve_text is not None:
                    # A Sieve filter: Edit opens the code editor, not the
                    # wizard. The script itself is the whole filter.
                    raw["sieve_text"] = sieve_text
                    is_sieve = True
                else:
                    # Wizard opens on Name step - click Next to go to Conditions
                    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
                    if not next_btn:
                        issues.append("filter wizard did not open (no Next button)")
                    else:
                        await next_btn.click()
                        await page.wait_for_timeout(MODAL_TRANSITION_MS)

                        conditions, condition_issues = await self._scrape_conditions(page=page)
                        issues.extend(condition_issues)
                        logic = await self._scrape_logic(page=page)
                        raw["conditions_text"] = await self._read_step_text(
                            page, [selectors.FILTER_CONDITION_ROWS, selectors.FILTER_MODAL_NEXT],
                        )
                        if not raw["conditions_text"]:
                            issues.append("could not capture the Conditions step's raw text")

                        # Click Next to go to Actions step
                        next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
                        if not next_btn:
                            issues.append("could not reach the Actions step (no Next button)")
                        else:
                            await next_btn.click()
                            await page.wait_for_timeout(MODAL_TRANSITION_MS)
                            # Evidence first: reading the actions can close
                            # the wizard (see _scrape_actions).
                            raw["actions_text"] = await self._read_step_text(
                                page, KNOWN_ACTION_ROW_SELECTORS + [selectors.FILTER_MODAL_NEXT],
                            )
                            if not raw["actions_text"]:
                                issues.append("could not capture the Actions step's raw text")
                            actions, action_issues = await self._scrape_actions(page=page)
                            issues.extend(action_issues)
        except Exception as e:
            issues.append(f"error while reading the filter wizard: {e}")

        try:
            close_btn = await page.query_selector(
                f'{selectors.FILTER_MODAL_CLOSE}, {selectors.CANCEL_BUTTON}'
            )
            # The folder map build presses Escape, which already closed it;
            # clicking a hidden button would wait out Playwright's timeout.
            if close_btn and await close_btn.is_visible():
                await close_btn.click()
                await page.wait_for_timeout(DROPDOWN_MS)
        except Exception as e:
            logger.debug("Could not close edit modal for filter '%s': %s", name, e)

        for issue in issues:
            logger.warning("Filter '%s' incomplete: %s", name, issue)

        return {
            "name": name,
            "enabled": enabled,
            "priority": idx,
            "logic": logic,
            "conditions": conditions,
            "actions": actions,
            "raw": raw,
            "scrape_issues": issues,
            "is_sieve": is_sieve,
        }

    async def _read_sieve_editor(self, page: Page) -> Optional[str]:
        """Return the Sieve editor's script if Edit opened it, else None."""
        editor = await page.query_selector(selectors.SIEVE_EDITOR_CM)
        if not editor or not await editor.is_visible():
            return None
        return await page.evaluate(
            "() => { const cm = document.querySelector('.CodeMirror'); "
            "return cm && cm.CodeMirror ? cm.CodeMirror.getValue() : cm.innerText; }"
        )

    async def _read_step_text(self, page: Page, anchor_selectors: List[str]) -> str:
        """Capture the current wizard step's visible text as raw evidence.

        Finds the modal around the first matching anchor (a row of this step,
        else the Next button) and returns its innerText plus the state of its
        visible form fields, which innerText does not include (text input
        values, checkbox/radio states, dropdown aria-labels).

        Returns "" when no anchor is found or the anchor is not inside a
        modal, never the page body: that would capture the whole settings
        page. Callers record "" as a scrape issue. Never raises: evidence
        capture must not be the thing that breaks a scrape.
        """
        try:
            for anchor_selector in anchor_selectors:
                anchor = await page.query_selector(anchor_selector)
                if anchor:
                    text = await anchor.evaluate(STEP_TEXT_JS)
                    if not text:
                        logger.debug("Step anchor %s is not inside a modal", anchor_selector)
                    return text
            logger.debug("No step anchor found among %s", anchor_selectors)
            return ""
        except Exception as e:
            logger.debug("Could not capture step text: %s", e)
            return ""

    async def _scrape_conditions(self, page: Page = None) -> Tuple[List[dict], List[str]]:
        """Scrape conditions from the Conditions step of the filter wizard.

        Returns (conditions, issues). A condition that cannot be read, or
        whose type/operator is not one the model knows, is an issue rather
        than a guessed default: a dropped or misread condition widens what a
        filter matches, which for a delete rule means deleting more mail.
        """
        if page is None:
            page = self.page
        conditions = []
        issues = []

        condition_rows = await page.query_selector_all(selectors.FILTER_CONDITION_ROWS)
        if not condition_rows:
            issues.append("Conditions step has no condition rows")

        for row_index, row in enumerate(condition_rows):
            try:
                select_btns = await row.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
                type_label = await select_btns[0].get_attribute("aria-label") if select_btns else None
                operator_label = (
                    await select_btns[1].get_attribute("aria-label") if len(select_btns) >= 2 else None
                )
                if not type_label or not operator_label:
                    issues.append(f"condition {row_index}: type/operator dropdown not readable")
                    continue

                cond_type = type_label.lower().strip()
                operator = operator_label.lower().strip()
                cond_type = UI_TYPE_TO_MODEL.get(cond_type, cond_type)
                operator = UI_OPERATOR_TO_MODEL.get(operator, operator)
                if cond_type not in KNOWN_CONDITION_TYPES:
                    issues.append(f"condition {row_index}: unknown condition type {type_label!r}")
                if operator not in KNOWN_OPERATORS:
                    issues.append(f"condition {row_index}: unknown operator {operator_label!r}")

                # Get values - check for tags/chips first, then input. Each
                # chip is its own value (the condition matches if any does),
                # so several chips are kept as a list, never joined into
                # text that would later have to be split again.
                values: List[str] = []
                tags = await row.query_selector_all(selectors.CONDITION_VALUE_TAGS)
                if tags:
                    for tag in tags:
                        values.append((await tag.inner_text()).strip())
                else:
                    value_el = await row.query_selector(selectors.CONDITION_VALUE_INPUT)
                    if value_el:
                        values.append((await value_el.input_value()).strip())
                if cond_type != "attachments" and (not values or not all(values)):
                    issues.append(f"condition {row_index}: no value found")

                condition = {"type": cond_type, "operator": operator}
                if len(values) > 1:
                    condition["values"] = values
                else:
                    condition["value"] = values[0] if values else ""
                conditions.append(condition)
            except Exception as e:
                issues.append(f"condition {row_index}: could not be read ({e})")

        return conditions, issues

    async def _scrape_actions(self, page: Page = None) -> Tuple[List[dict], List[str]]:
        """Scrape actions from the Actions step of the filter wizard.

        Returns (actions, issues). Understood rows are folder, label,
        mark-as and auto-reply (read only to confirm it is off). Any other
        visible action row is an issue, as is a known row that cannot be
        read: an action the scraper does not record would be missing from
        the Sieve script and lost when the filter is deleted.

        The folder row is read LAST. Building the folder path map opens the
        folder dropdown and presses Escape, which in the live UI closes the
        whole wizard, so nothing in this step can be read after it.
        """
        if page is None:
            page = self.page
        actions = []
        issues = []

        # Any visible action row we do not know how to read
        for row in await page.query_selector_all(selectors.FILTER_ACTION_ANY_ROW):
            testid = await row.get_attribute("data-testid")
            if testid not in KNOWN_ACTION_ROW_TESTIDS and await row.is_visible():
                issues.append(f"unsupported action row {testid!r}")

        # Read the folder selection now, but resolve it (which may open the
        # dropdown and close the wizard) only after the other rows.
        folder_row = await page.query_selector(selectors.FILTER_ACTION_FOLDER_ROW)
        folder_btn = None
        folder_label = None
        if folder_row:
            # button.select, not the row's first button: that one is the
            # "Move to" collapse toggle.
            folder_btn = await folder_row.query_selector(selectors.FOLDER_SELECT_BUTTON)
            folder_label = await folder_btn.get_attribute("aria-label") if folder_btn else None
            if not folder_label:
                issues.append("folder row has no readable dropdown")

        # Check "Label as" selection (a filter can apply several labels)
        label_row = await page.query_selector(selectors.FILTER_ACTION_LABEL_ROW)
        label_actions = []
        if label_row:
            labels, issue = await self._read_label_row(label_row)
            if issue:
                issues.append(issue)
            for label in labels:
                label_actions.append({"type": "label", "parameters": {"label": label}})

        # Check "Mark as" checkboxes
        mark_row = await page.query_selector(selectors.FILTER_ACTION_MARK_AS_ROW)
        mark_actions = []
        if mark_row:
            read_cb = await mark_row.query_selector(selectors.MARK_READ_CHECKBOX)
            if read_cb and await read_cb.evaluate(CHECKED_JS):
                mark_actions.append({"type": "mark_read", "parameters": {}})
            star_cb = await mark_row.query_selector(selectors.MARK_STARRED_CHECKBOX)
            if star_cb and await star_cb.evaluate(CHECKED_JS):
                mark_actions.append({"type": "star", "parameters": {}})
            # Any other ticked box in the row is a mark-as we do not model
            for checkbox in await mark_row.query_selector_all('input[type="checkbox"]'):
                box_label = await checkbox.evaluate(CHECKBOX_LABEL_JS)
                if box_label.lower() not in KNOWN_MARK_AS_LABELS and await checkbox.evaluate(CHECKED_JS):
                    issues.append(f"unsupported mark-as option {box_label!r} is checked")

        # Auto-reply: on every filter, off by default. We cannot express it
        # in Sieve, so a filter using it must not be treated as fully read.
        auto_reply_row = await page.query_selector(selectors.FILTER_ACTION_AUTO_REPLY_ROW)
        if auto_reply_row:
            toggle = await auto_reply_row.query_selector('input[type="checkbox"]')
            if not toggle:
                issues.append("auto-reply row has no readable toggle")
            elif await toggle.evaluate(CHECKED_JS):
                issues.append("auto-reply action not supported")

        # The live UI renders all four rows on every filter, so a missing one
        # means the step did not render as expected (or the wizard closed);
        # treating it as "no such action" is how labels were lost before.
        for row, row_name in (
            (folder_row, "folder"), (label_row, "label"),
            (mark_row, "mark-as"), (auto_reply_row, "auto-reply"),
        ):
            if not row:
                issues.append(f"Actions step has no {row_name} row")

        # Folder last (see docstring)
        if folder_label and folder_label.strip() != "Do not move":
            if self._folder_path_map is None:
                await self._build_folder_path_map(folder_btn, page=page)

            folder = self._resolve_folder_path(folder_label)
            # System folders (Trash, Archive, Spam, Inbox) become the action
            # Proton's own Sieve generator uses for them; Trash is a folder
            # move, never a permanent delete.
            system_action = SYSTEM_FOLDER_ACTIONS.get(folder)
            if system_action is not None:
                actions.append(copy.deepcopy(system_action))
            else:
                actions.append({"type": "move_to", "parameters": {"folder": folder}})

        return actions + label_actions + mark_actions, issues

    async def _read_label_row(self, label_row) -> Tuple[List[str], Optional[str]]:
        """Read the applied labels from the Actions step's "Label as" row.

        The only DOM-reading code for labels. Shape captured from the live
        UI on 2026-09-30 (Add/Edit filter wizard, Actions step):

            <div data-testid="filter-modal:label-row">
              <button type="button">...<span>Label as</span></button>   <- collapse toggle
              <div class="w-full"><div class="w-full">
                <div class="mb-2 inline-block text-ellipsis">
                  <label class="checkbox-container ..." title="NAME">
                    <input type="checkbox" class="checkbox-input">
                    ...<ul class="label-stack"><li class="label-stack-item">
                         <span class="label-stack-item-text">NAME</span></li></ul>
                  </label>
                </div>
                ... one per label that EXISTS on the account ...
              </div>
              <button type="button">Create label</button></div>
            </div>

        Every account label is listed, each with a chip, so chips say
        nothing about what is applied. Applied = options whose checkbox
        has the live `checked` PROPERTY set (the HTML attribute never
        changes). Name = the <label>'s title, else its chip text.
        """
        options = []
        option_elements = await label_row.query_selector_all(selectors.FILTER_LABEL_OPTION)
        for option in option_elements:
            name = await option.get_attribute("title")
            if not name:
                chip = await option.query_selector(selectors.FILTER_LABEL_OPTION_TEXT)
                name = await chip.inner_text() if chip else ""
            checkbox = await option.query_selector('input[type="checkbox"]')
            if not checkbox:
                return [], f"label option {name!r} has no checkbox"
            options.append((name, await checkbox.evaluate(CHECKED_JS)))

        # A checkbox outside the known option shape could be a selected
        # label we would otherwise never see.
        all_checkboxes = await label_row.query_selector_all('input[type="checkbox"]')
        if len(all_checkboxes) != len(option_elements):
            return [], (
                f"label row has {len(all_checkboxes)} checkboxes but "
                f"{len(option_elements)} label options"
            )

        row_text = await label_row.inner_text()
        return _parse_label_row(options, row_text)

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

            # Close the dropdown by pressing Escape. In the live UI this
            # closes the whole filter wizard too, so callers must read
            # everything else in the step first (see _scrape_actions).
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
