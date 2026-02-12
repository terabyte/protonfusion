"""CSS selectors for ProtonMail UI elements.

All UI selectors are centralized here. When ProtonMail changes their UI,
update these selectors and run the E2E test to verify.

Settings are served from account.proton.me, accessed via the settings gear icon
in the mail app at mail.proton.me.
"""

# Login page
USERNAME_INPUT = 'input[id="username"]'
PASSWORD_INPUT = 'input[id="password"]'
LOGIN_BUTTON = 'button[type="submit"]'

# Navigation - after login
SETTINGS_GEAR = '[data-testid="settings-drawer-app-button:settings-icon"]'
ALL_SETTINGS_LINK = 'a:has-text("All settings")'
FILTERS_NAV_LINK = 'a[href="/u/0/mail/filters"]'
COMPOSE_BUTTON = '[data-testid="sidebar:compose"]'
USER_DROPDOWN_EMAIL = '[data-testid="heading:userdropdown"] span.user-dropdown-displayName + span'

# Filter list page (account.proton.me/u/0/mail/filters)
# Page structure: two <section> blocks, each with an <h2>.
#   Section 1: h2 "Custom filters"   -> user-created filters (table.simple-table)
#   Section 2: h2 "Spam, block, and allow lists" -> spam/allow entries
# We must only scrape from section 1.
PAGE_HEADING = '.container-section-sticky h1'  # should say "Filters"
CUSTOM_FILTERS_HEADING = 'h2:has-text("Custom filters")'
SPAM_LISTS_HEADING = 'h2:has-text("Spam, block, and allow lists")'
CUSTOM_FILTERS_SECTION = 'section:has(h2:has-text("Custom filters"))'
ADD_FILTER_BUTTON = 'button:has-text("Add filter")'
FILTER_ACTIONS_DROPDOWN = '[data-testid="dropdownActions:dropdown"]'
FILTER_TABLE = 'table.simple-table'
FILTER_TABLE_ROWS = 'table.simple-table tbody tr'
FILTER_EDIT_BUTTON = 'button[aria-label*="Edit filter"]'
FILTER_EDIT_BUTTON_ALT = 'button:has-text("Edit")'
FILTER_TOGGLE = 'input[type="checkbox"], .toggle-label input'
FILTER_TOGGLE_LABEL = 'label[data-testid="toggle-switch"]'
FILTER_NAME_FALLBACK = '.text-ellipsis, [title], span'

# Filter creation wizard - Name step
FILTER_MODAL_NAME = '[data-testid="filter-modal:name-input"]'
FILTER_MODAL_NEXT = '[data-testid="filter-modal:next-button"]'
FILTER_MODAL_CLOSE = '[data-testid="modal:close"]'

# Filter creation wizard - Conditions step
FILTER_CONDITION_ROW = '[data-testid="filter-modal:condition-0"]'
FILTER_CONDITION_ROW_N = '[data-testid="filter-modal:condition-{}"]'
FILTER_CONDITION_ROWS = '[data-testid*="filter-modal:condition"]'

# Filter creation wizard - Actions step
FILTER_ACTION_FOLDER_ROW = '[data-testid="filter-modal:folder-row"]'
FILTER_ACTION_LABEL_ROW = '[data-testid="filter-modal:label-row"]'
FILTER_ACTION_MARK_AS_ROW = '[data-testid="filter-modal:mark-as-row"]'

# Sieve editor modal - opened by "Add sieve filter" button or editing an existing sieve filter
ADD_SIEVE_FILTER_BUTTON = 'button:has-text("Add sieve filter")'
SIEVE_EDITOR_CM = '.CodeMirror'  # CodeMirror 5 wrapper (use CM5 JS API to read/write)
SIEVE_FILTER_NAME_INPUT = '[data-testid="filter-modal:name-input"], input[placeholder="Name"]'
SIEVE_SAVE_BUTTON = 'button:has-text("Save"), button[type="submit"]'

# Legacy selectors kept for compatibility
SIEVE_TAB = ADD_SIEVE_FILTER_BUTTON
SIEVE_EDITOR = '.CodeMirror textarea, textarea'

# Dialogs
LOADING_SPINNER = '.loading-animation'
DELETE_CONFIRM_DIALOG = 'dialog.prompt'
DELETE_CONFIRM_BUTTON = 'dialog.prompt button:has-text("Delete")'

# Custom dropdown interaction (Proton uses button.select + li.dropdown-item)
CUSTOM_SELECT_BUTTON = 'button.select'
DROPDOWN_ITEM = 'li.dropdown-item'

# Value input elements within condition rows
CONDITION_VALUE_TAGS = '.condition-token [title]'
CONDITION_VALUE_INPUT = 'input[type="text"]'
CONDITION_INSERT_BUTTON = 'button:has-text("Insert")'

# Mark as checkboxes
MARK_READ_CHECKBOX = 'label:has-text("Read") input[type="checkbox"]'
MARK_READ_LABEL = 'label:has-text("Read")'
MARK_STARRED_CHECKBOX = 'label:has-text("Starred") input[type="checkbox"]'
MARK_STARRED_LABEL = 'label:has-text("Starred")'

# Move to folder selector (in Actions step)
FOLDER_SELECT = (
    'button.select[aria-label="Do not move"], '
    'button.select[aria-label*="move"]'
)
SAVE_BUTTON = 'button:has-text("Save")'
CANCEL_BUTTON = 'button:has-text("Cancel")'
