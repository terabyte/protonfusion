# ProtonMail UI Notes

This document describes the ProtonMail web UI structure as it relates to ProtonFusion's browser automation. These details are useful for maintaining the scraper when ProtonMail updates their interface.

## Navigation Path

ProtonFusion navigates to the filters page through this sequence:

1. Login at `account.proton.me/login`
2. Wait for redirect to inbox (URL contains `/apps` or `/mail`)
3. Click the settings gear icon
4. Click "All settings"
5. Click "Filters" in the sidebar

Direct navigation to settings URLs (e.g., `mail.proton.me/settings/filters`) no longer works -- ProtonMail moved settings to `account.proton.me`.

## Filters Page Structure

The filters settings page has this layout:

```
┌─────────────────────────────────────────┐
│ h1: "Filters"                           │  (.container-section-sticky h1)
├─────────────────────────────────────────┤
│ section: "Custom filters" (h2)          │  ← scraper targets this section
│ ┌─────────────────────────────────────┐ │
│ │ table.simple-table                  │ │
│ │ ┌─────────────────────────────────┐ │ │
│ │ │ Row: filter name | toggle | Edit│ │ │
│ │ │ Row: filter name | toggle | Edit│ │ │
│ │ └─────────────────────────────────┘ │ │
│ │ [Add filter] [Add sieve filter]   │ │
│ └─────────────────────────────────────┘ │
├─────────────────────────────────────────┤
│ section: "Spam, block, and allow lists" │  ← scraper ignores this section
│ (h2)                                    │
│ ┌─────────────────────────────────────┐ │
│ │ Spam list table                    │ │
│ │ Block list table                   │ │
│ │ Allow list table                   │ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

The scraper scopes all queries to the "Custom filters" section (`section:has(h2:has-text("Custom filters"))`) to avoid picking up entries from the Spam/Block/Allow section.

## Filter Table

Filter rows are in `table.simple-table tbody tr` within the Custom filters section. Each row contains:
- Filter name (text content)
- Enable/disable toggle
- "Edit" button (the filter name is in the button's `aria-label`)

## Filter Edit Wizard

Clicking "Edit" opens a multi-step wizard modal:

**Step 1: Name**
- Filter name input field

**Step 2: Conditions**
- Logic selector (ALL / ANY conditions)
- Condition rows, each with:
  - Type dropdown (`button.select` → `li.dropdown-item`): Sender, Recipient, Subject, Attachments
  - Operator dropdown: contains, is exactly, matches, begins with, ends with
  - Value input field

**Step 3: Actions**
- Action rows, each with:
  - Type dropdown: Move to, Label as, Mark as read, Star, Archive, Permanently delete
  - Parameter (folder/label selector, when applicable)

Every filter's Actions step has four rows, identified by `data-testid`
(checked against the live UI on 2026-09-30): `filter-modal:folder-row`,
`filter-modal:label-row`, `filter-modal:mark-as-row` and
`filter-modal:auto-reply-row`. Any other visible `filter-modal:*-row` marks the
filter incomplete, and so does a missing one of the four. An "Apply filter to existing emails" checkbox sits outside
the rows; it is a one-time action on save, not part of the filter, and is
ignored.

- **Collapse toggles.** The first `<button>` in the folder, label and mark-as
  rows is a section collapse toggle ("Move to", "Label as", "Mark as"), not a
  dropdown. The folder dropdown is `button.select` (`id="move-to-select"`), whose
  `aria-label` is the selected folder ("Do not move" when none).
- **"Label as" row.** It lists **every label on the account**, each as
  `<label class="checkbox-container" title="NAME">` holding an
  `input.checkbox-input` and a `label-stack` chip with the name. Applied labels
  are the ticked ones. Row chrome is "Label as" and "Create label".
- **Checked is a property.** Ticking a box sets the DOM `checked` property only;
  the HTML attribute never changes, so the serialized HTML looks the same
  whether a box is ticked or not. The scraper reads `el => el.checked`.
- **Auto-reply row.** "Send auto-reply" with a toggle switch, off by default. On
  means an action ProtonFusion cannot express, so the filter is flagged.
- **Escape closes the wizard.** Building the folder path map opens the folder
  dropdown and presses Escape, which closes the whole modal, so the scraper
  reads the folder row last.

Dropdowns use `button.select` to open and `li.dropdown-item` for options (not native `<select>` elements).

## Sieve Editor

"Add sieve filter" is a button, not a tab. It opens a modal with:
- Filter name input
- CodeMirror 5 editor (`div.CodeMirror`, **not** a contenteditable div)
- Cancel / Save buttons

The editor's default content is ProtonMail's built-in spam-check script (not empty).

### CodeMirror 5 Interaction

- **Read**: `document.querySelector('.CodeMirror').CodeMirror.getValue()`
- **Write**: `document.querySelector('.CodeMirror').CodeMirror.setValue(script)`
- Keyboard input does **not** trigger change detection (Save stays disabled)
- The JavaScript API properly triggers change events

## Free Tier Limitation

On the free plan, only 1 custom filter is allowed. After creating one:
- "Add filter" button disappears
- "Add sieve filter" button disappears
- A "Get more filters" upsell replaces both buttons

The sync workflow disables existing UI filters first to free the slot.

## Delete Confirmation

Deleting a filter shows a confirmation dialog. The confirm button is at: `dialog.prompt button:has-text("Delete")` (no data-testid).

## Folder Dropdown Hierarchy

When a filter action involves moving to a folder, the folder dropdown displays:
- Top-level folders without prefix: `Inbox`, `Work`, `Personal`
- Subfolders with bullet prefix: `• Project A`, `• Urgent`

The bullet character is `•` (U+2022). The scraper strips this prefix and builds a path map from the display order to resolve full paths like `Work/Project A`.

## Settings Drawer Overlay

When navigating from `account.proton.me` back to the inbox, the Settings drawer may remain visible and intercept clicks on the gear icon. The workaround is to use JavaScript `evaluate("el => el.click()")` instead of Playwright's `.click()` method to bypass the overlay.

## Structural Validation

The scraper calls `_assert_filter_page_structure()` before scraping. This validates:
- The page has an h1 with "Filters"
- There is a "Custom filters" section with an h2
- There is a "Spam, block, and allow lists" section with an h2
- The filter table exists within the Custom filters section

If any assertion fails, scraping aborts with a clear error message indicating what changed.

## Updating Selectors

When ProtonMail changes their UI:

1. Open the filters page manually (Settings → All settings → Filters)
2. Use browser dev tools to inspect the new element structure
3. Update `src/scraper/selectors.py`
4. Run the E2E test: `bash test_workflow.sh`
