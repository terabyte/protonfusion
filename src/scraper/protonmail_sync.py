"""Playwright automation for sync/restore operations on ProtonMail."""

import logging
import re
from typing import Dict, List, Optional, Sequence

from src.scraper import selectors
from src.scraper.browser import (
    ProtonMailBrowser, MODAL_TRANSITION_MS, DROPDOWN_MS,
    ALL_SETTINGS_LOAD_MS, SieveReadError, row_filter_name,
)
from src.utils.config import ELEMENT_TIMEOUT_MS, loggable_text

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

# The wizard's "Move to" dropdown label for each system folder, keyed the
# way an action records it (see SYSTEM_FOLDER_ACTIONS): Trash and Archive
# by action type, Spam and Inbox by their move_to folder, Proton's Sieve name.
MOVE_TO_LABELS = {
    "trash": "Trash",
    "archive": "Archive",
    "spam": "Spam",
    "inbox": "Inbox - Default",
}

# A "/" not preceded by a backslash: the separator in a folder path.
_FOLDER_SEPARATOR = re.compile(r"(?<!\\)/")


def move_to_dropdown_label(action: Dict) -> Optional[str]:
    r"""The "Move to" dropdown entry that applies `action`, or None for other actions.

    `action` is shaped like a FilterAction: {"type": ..., "parameters": {...}}.
    A move_to folder is stored as an escaped path ("Work/Misc\/Others");
    the dropdown lists each folder under its own name, so this returns the
    last segment with its "\/" unescaped ("Misc/Others").
    """
    action_type = action.get("type", "")
    if action_type in MOVE_TO_LABELS:
        return MOVE_TO_LABELS[action_type]
    if action_type != "move_to":
        return None
    folder = (action.get("parameters") or {}).get("folder", "inbox")
    if folder in MOVE_TO_LABELS:
        return MOVE_TO_LABELS[folder]
    return _FOLDER_SEPARATOR.split(folder)[-1].replace("\\/", "/")


logger = logging.getLogger(__name__)

# Puts the script into the Sieve editor through the CodeMirror 5 API (which
# fires the change events the Save button listens for) and says whether it
# could: "no-editor" when the page has no attached CodeMirror instance.
_SET_EDITOR_SCRIPT_JS = """(script) => {
    const cm = document.querySelector('.CodeMirror');
    if (!cm || !cm.CodeMirror) {
        return 'no-editor';
    }
    cm.CodeMirror.setValue(script);
    return 'ok';
}"""


def normalize_script(text: str) -> str:
    """A script with trailing whitespace removed from each line and from the whole text.

    The read-back comparison ignores only that: an editor may trim it, and
    read_sieve_script strips the text it returns. Anything else that differs
    means the saved script is not the one intended.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


class ProtonMailSync(ProtonMailBrowser):
    """Handles sync operations: create/delete/toggle filters, upload Sieve."""

    # Set by upload_sieve when it could not create the Sieve filter because
    # the "Add sieve filter" button was gone, which is how ProtonMail shows
    # an account at its active-filter limit. Lets the caller say so.
    upload_hit_filter_limit = False

    async def upload_sieve(
        self, sieve_script: str, filter_name: str = "ProtonFusion Consolidated",
    ) -> bool:
        """Upload a Sieve script as a named sieve filter, then prove it landed.

        Creates a new sieve filter or updates an existing one, setting the
        editor through the CodeMirror 5 JavaScript API. Returns True only
        once the saved filter has been opened again, its script read back
        and found equal to `sieve_script` (ignoring trailing whitespace), and
        the filter is enabled. Any step that fails returns False, so the
        caller takes its failed-upload path rather than reporting success
        for a save that did not happen.
        """
        page = self.page
        self.upload_hit_filter_limit = False

        try:
            # Try to find and edit an existing filter with this name
            editing_existing = await self._open_sieve_filter_by_name(filter_name)

            if not editing_existing:
                # Create new: click "Add sieve filter"
                add_btn = await page.query_selector(selectors.ADD_SIEVE_FILTER_BUTTON)
                if not add_btn or not await add_btn.is_visible():
                    logger.error(
                        "'Add sieve filter' button not available; the account "
                        "is probably at its active-filter limit."
                    )
                    self.upload_hit_filter_limit = True
                    return False

                await add_btn.click()
                await page.wait_for_timeout(ALL_SETTINGS_LOAD_MS)

                # Fill the filter name
                name_input = await page.query_selector(selectors.SIEVE_FILTER_NAME_INPUT)
                if name_input:
                    await name_input.fill(filter_name)
                    await page.wait_for_timeout(DROPDOWN_MS)

            # Wait for CodeMirror to initialize
            try:
                await page.wait_for_selector(
                    selectors.SIEVE_EDITOR_CM, timeout=ELEMENT_TIMEOUT_MS,
                )
            except Exception:
                logger.error("CodeMirror editor not found")
                return False

            # Set content via CodeMirror 5 API (triggers proper change events)
            status = await page.evaluate(_SET_EDITOR_SCRIPT_JS, sieve_script)
            if status != "ok":
                logger.error("The Sieve editor has no CodeMirror instance; the script was not set")
                return False
            await page.wait_for_timeout(DROPDOWN_MS)

            # Wait for Save button to become enabled, then click it
            save_btn = await page.query_selector(selectors.SIEVE_SAVE_BUTTON)
            if save_btn:
                # Wait up to 5s for the button to enable
                for _ in range(10):
                    if not await save_btn.is_disabled():
                        break
                    await page.wait_for_timeout(500)

                if await save_btn.is_disabled():
                    logger.warning("Save button still disabled after setting content")
                    return False

                await save_btn.click()
                await page.wait_for_timeout(3000)

                # Ensure the filter is enabled after save
                await self._ensure_filter_enabled(filter_name)
                if not await self._verify_upload(sieve_script, filter_name):
                    return False
                logger.info("Sieve script uploaded and verified")
                return True

            logger.warning("Could not find save button for Sieve editor")
            return False

        except Exception as e:
            logger.error("Failed to upload Sieve script: %s", loggable_text(str(e)))
            raise

    async def _verify_upload(self, intended: str, filter_name: str) -> bool:
        """True if the saved filter holds `intended` and is enabled.

        Opens the filter again and reads its script back, since the Save
        button enabling and being clicked says nothing about whether the
        save went through.
        """
        try:
            saved = await self.read_sieve_script(filter_name=filter_name)
        except SieveReadError as e:
            logger.error("Could not read the Sieve filter back after saving: %s", loggable_text(str(e)))
            return False
        if normalize_script(saved) != normalize_script(intended):
            logger.error(
                "The saved Sieve filter '%s' does not hold the uploaded script "
                "(read back %d chars, expected %d)", filter_name, len(saved), len(intended),
            )
            return False
        if not await self._filter_is_enabled(filter_name):
            logger.error("The Sieve filter '%s' is not enabled after saving", filter_name)
            return False
        return True

    async def _filter_is_enabled(self, name: str) -> bool:
        """True if the one filter named exactly `name` has its toggle on."""
        section = await self.page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
        if not section:
            logger.warning("Custom filters section not found")
            return False
        row = await self._unique_row_named(section, name)
        if row is None:
            return False
        toggle_input = await row.query_selector(selectors.FILTER_TOGGLE)
        return bool(toggle_input) and await toggle_input.is_checked()

    async def _ensure_filter_enabled(self, name: str):
        """Enable the one filter with exactly this name if it isn't already enabled.

        Does nothing (with a warning) when no row or more than one row has the
        name, so another filter sharing it is never switched on by mistake.
        """
        page = self.page
        section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
        if not section:
            return
        row = await self._unique_row_named(section, name)
        if row is None:
            return
        toggle_input = await row.query_selector(selectors.FILTER_TOGGLE)
        if toggle_input and not await toggle_input.is_checked():
            toggle_label = await row.query_selector(selectors.FILTER_TOGGLE_LABEL)
            if toggle_label:
                await toggle_label.click()
                await page.wait_for_timeout(1000)
                logger.info("Enabled filter: %s", name)

    async def _unique_row_named(self, section, name: str):
        """The single Custom filters row named exactly `name`, or None (with a warning).

        None when no row has the name, and also when several do: rows can
        only be told apart by name here, so picking one would be a guess.
        """
        rows = await section.query_selector_all(selectors.FILTER_TABLE_ROWS)
        matches = [row for row in rows if await self._get_filter_name(row) == name]
        if not matches:
            logger.warning("Filter '%s' not found", name)
            return None
        if len(matches) > 1:
            logger.warning("%d filters are named '%s'; not touching any of them", len(matches), name)
            return None
        return matches[0]

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
            actions: List of dicts shaped like FilterAction: type (an
                ActionType value) and, for move_to, parameters["folder"]
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

                folder_name = move_to_dropdown_label(action)
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
            logger.error("Failed to create filter '%s': %s", name, loggable_text(str(e)))
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
        """Set the toggle of the one filter named exactly `name`.

        Returns False without clicking when no row, or more than one row,
        has that name: with a shared name the wrong filter could be toggled.
        """
        page = self.page
        section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
        if not section:
            logger.warning("Custom filters section not found")
            return False
        row = await self._unique_row_named(section, name)
        if row is None:
            return False

        toggle_input = await row.query_selector(selectors.FILTER_TOGGLE)
        toggle_label = await row.query_selector(selectors.FILTER_TOGGLE_LABEL)
        if not toggle_input or not toggle_label:
            logger.warning("No toggle for filter '%s'", name)
            return False
        if await toggle_input.is_checked() != enabled:
            await toggle_label.click()
            await page.wait_for_timeout(1000)
            logger.info("%s filter: %s", "Enabled" if enabled else "Disabled", name)
        else:
            logger.info("Filter '%s' already %s", name, "enabled" if enabled else "disabled")
        return True

    async def set_row_enabled(
        self, index: int, name: str, enabled: bool, *,
        expected_names: Optional[Sequence[str]] = None,
        require_current: Optional[bool] = None,
    ) -> bool:
        """Set the toggle of one Custom filters row, identified by position and name.

        `index` is the row's position when it was scraped (the filter's
        priority) and `name` its name then. Rows can share a name, so the
        position picks the row and the name confirms it. The position is
        trusted only when the row there has that name and, if
        `expected_names` (every row's name, in order, at scrape time) is
        given, the list as a whole is unchanged: otherwise a list that moved
        could put a neighbour with the same name at that position. When the
        position is not trusted, a row is used only if it is the single row
        with that name.

        `require_current`, if given, is the state the row must be in before
        the click (True when disabling a filter that was scraped as enabled).
        A row in any other state is refused, so a filter the user switched
        off since the scrape is never recorded as one this run disabled, and
        so never switched on by a failed sync's re-enable.

        Returns True once the row is in the requested state; False, without
        clicking, whenever the row cannot be identified with certainty.
        """
        page = self.page
        section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
        if not section:
            logger.warning("Custom filters section not found; not toggling '%s'", name)
            return False
        rows = await section.query_selector_all(selectors.FILTER_TABLE_ROWS)
        names = [await self._get_filter_name(r) for r in rows]

        list_unchanged = expected_names is None or list(expected_names) == names
        row = None
        if list_unchanged and 0 <= index < len(rows) and names[index] == name:
            row = rows[index]
        else:
            same_name = [r for r, n in zip(rows, names) if n == name]
            if len(same_name) == 1:
                row = same_name[0]
        if row is None:
            logger.warning("Filter '%s' (row %d) not found unambiguously; not toggling it", name, index)
            return False

        toggle_input = await row.query_selector(selectors.FILTER_TOGGLE)
        toggle_label = await row.query_selector(selectors.FILTER_TOGGLE_LABEL)
        if not toggle_input or not toggle_label:
            logger.warning("No toggle for filter '%s'", name)
            return False
        is_checked = await toggle_input.is_checked()
        if require_current is not None and is_checked != require_current:
            logger.warning(
                "Filter '%s' is %s, not %s as scraped; not toggling it", name,
                "enabled" if is_checked else "disabled", "enabled" if require_current else "disabled",
            )
            return False
        if is_checked != enabled:
            await toggle_label.click()
            await page.wait_for_timeout(1000)
        logger.info("%s filter: %s", "Enabled" if enabled else "Disabled", name)
        return True

    async def delete_filter(self, name: str) -> bool:
        """Delete one disabled filter by name from the Custom filters section.

        Rows can only be told apart by name, so this refuses (returns False,
        deletes nothing) unless exactly one row has that name and its toggle
        is off. With a shared name the wrong filter could go, and an enabled
        filter is live mail handling that cleanup never means to delete.
        """
        page = self.page
        section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
        if not section:
            logger.warning("Custom filters section not found; not deleting '%s'", name)
            return False
        rows = await section.query_selector_all(selectors.FILTER_TABLE_ROWS)

        matches = []
        for row in rows:
            if await self._get_filter_name(row) == name:
                matches.append(row)
        if not matches:
            logger.warning("Filter '%s' not found for deletion", name)
            return False
        if len(matches) > 1:
            logger.warning("%d filters are named '%s'; not deleting any of them", len(matches), name)
            return False

        row = matches[0]
        toggle_input = await row.query_selector(selectors.FILTER_TOGGLE)
        if not toggle_input or await toggle_input.is_checked():
            logger.warning("Filter '%s' is enabled (or its toggle is unreadable); not deleting", name)
            return False

        dropdown = await row.query_selector(selectors.FILTER_ACTIONS_DROPDOWN)
        if not dropdown:
            logger.warning("No actions menu for filter '%s'; not deleting", name)
            return False
        await dropdown.click()
        await page.wait_for_timeout(DROPDOWN_MS)

        delete_item = await page.query_selector(
            f'{selectors.DROPDOWN_ITEM}:has-text("Delete")'
        )
        if not delete_item:
            logger.warning("No Delete option in the menu for filter '%s'", name)
            return False
        await delete_item.click()
        await page.wait_for_timeout(DROPDOWN_MS)

        if not await self._confirm_delete():
            return False
        logger.info("Deleted filter: %s", name)
        return True

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
        """Exact filter name of a table row (see row_filter_name)."""
        return await row_filter_name(row)
