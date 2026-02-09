"""CSS selectors for ProtonMail UI elements.

Update these if ProtonMail changes their UI. Settings are now served from
account.proton.me (not mail.proton.me), accessed via the settings gear icon.
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

# Filter settings page (account.proton.me/u/0/mail/filters)
ADD_FILTER_BUTTON = 'button:has-text("Add filter")'
FILTER_ACTIONS_DROPDOWN = '[data-testid="dropdownActions:dropdown"]'

# Filter creation wizard - Name step
FILTER_MODAL_NAME = '[data-testid="filter-modal:name-input"]'
FILTER_MODAL_NEXT = '[data-testid="filter-modal:next-button"]'
FILTER_MODAL_CLOSE = '[data-testid="modal:close"]'

# Filter creation wizard - Conditions step
FILTER_CONDITION_ROW = '[data-testid="filter-modal:condition-0"]'
FILTER_CONDITION_ROW_N = '[data-testid="filter-modal:condition-{}"]'

# Filter creation wizard - Actions step
FILTER_ACTION_FOLDER_ROW = '[data-testid="filter-modal:folder-row"]'
FILTER_ACTION_LABEL_ROW = '[data-testid="filter-modal:label-row"]'
FILTER_ACTION_MARK_AS_ROW = '[data-testid="filter-modal:mark-as-row"]'

# Sieve editor
SIEVE_TAB = '[data-testid="settings:sieve-editor"]'
SIEVE_EDITOR = '.cm-content'
SIEVE_SAVE_BUTTON = '[data-testid="filter:sieve:save-button"]'

# Misc
LOADING_SPINNER = '.loading-animation'
# The delete confirmation dialog uses a <dialog class="prompt"> element
DELETE_CONFIRM_DIALOG = 'dialog.prompt'
DELETE_CONFIRM_BUTTON = 'dialog.prompt button:has-text("Delete")'

# Custom dropdown interaction (Proton uses button.select + li.dropdown-item)
CUSTOM_SELECT_BUTTON = 'button.select'
DROPDOWN_ITEM = 'li.dropdown-item'
