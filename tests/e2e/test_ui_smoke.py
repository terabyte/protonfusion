"""E2E smoke tests verifying ProtonMail's filter UI hasn't changed.

These tests run against the live ProtonMail site and require credentials.
They verify that the CSS selectors and text strings ProtonFusion depends on
are still present in the UI.
"""

import pytest
import pytest_asyncio

from src.scraper import selectors

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Tier 1 - Page Structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_heading(protonmail_page):
    """h1 says 'Filters'."""
    page = protonmail_page.page
    h1 = await page.query_selector(selectors.PAGE_HEADING)
    assert h1 is not None, f"Selector {selectors.PAGE_HEADING} not found"
    text = (await h1.inner_text()).strip()
    assert text == "Filters", f"Expected 'Filters', got '{text}'"


@pytest.mark.asyncio
async def test_custom_filters_heading(protonmail_page):
    """h2 'Custom filters' exists."""
    page = protonmail_page.page
    h2 = await page.query_selector(selectors.CUSTOM_FILTERS_HEADING)
    assert h2 is not None, f"Selector {selectors.CUSTOM_FILTERS_HEADING} not found"


@pytest.mark.asyncio
async def test_spam_lists_heading(protonmail_page):
    """h2 'Spam, block, and allow lists' exists."""
    page = protonmail_page.page
    h2 = await page.query_selector(selectors.SPAM_LISTS_HEADING)
    assert h2 is not None, f"Selector {selectors.SPAM_LISTS_HEADING} not found"


@pytest.mark.asyncio
async def test_custom_filters_section_is_section_tag(protonmail_page):
    """Wrapper is a <section> element."""
    page = protonmail_page.page
    section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
    assert section is not None, f"Selector {selectors.CUSTOM_FILTERS_SECTION} not found"
    tag = await section.evaluate("el => el.tagName.toLowerCase()")
    assert tag == "section", f"Expected <section>, got <{tag}>"


@pytest.mark.asyncio
async def test_filter_table_exists(protonmail_page):
    """table.simple-table inside custom filters section."""
    page = protonmail_page.page
    section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
    assert section is not None, "Custom filters section not found"
    table = await section.query_selector(selectors.FILTER_TABLE)
    assert table is not None, f"Selector {selectors.FILTER_TABLE} not found in section"


@pytest.mark.asyncio
async def test_account_email_captured(protonmail_page):
    """Fixture's browser captured an email with '@'."""
    assert "@" in protonmail_page.account_email, (
        f"Expected email with '@', got '{protonmail_page.account_email}'"
    )


@pytest.mark.asyncio
async def test_add_filter_button(protonmail_page):
    """'Add filter' button present."""
    page = protonmail_page.page
    btn = await page.query_selector(selectors.ADD_FILTER_BUTTON)
    assert btn is not None, f"Selector {selectors.ADD_FILTER_BUTTON} not found"


@pytest.mark.asyncio
async def test_sieve_filter_button(protonmail_page):
    """'Add sieve filter' button present."""
    page = protonmail_page.page
    btn = await page.query_selector(selectors.ADD_SIEVE_FILTER_BUTTON)
    assert btn is not None, f"Selector {selectors.ADD_SIEVE_FILTER_BUTTON} not found"


# ---------------------------------------------------------------------------
# Tier 2 - Filter Wizard (skip if no filters on account)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def first_filter_row(protonmail_page):
    """Get the first filter row; skip if no filters exist on the account."""
    page = protonmail_page.page
    section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
    if not section:
        pytest.skip("Custom filters section not found")
    rows = await section.query_selector_all(selectors.FILTER_TABLE_ROWS)
    if not rows:
        pytest.skip("No filters on this account; skipping wizard tests")
    yield rows[0]


@pytest_asyncio.fixture
async def open_wizard(protonmail_page, first_filter_row):
    """Open the edit wizard for the first filter, close it on teardown."""
    page = protonmail_page.page
    edit_btn = await first_filter_row.query_selector(selectors.FILTER_EDIT_BUTTON)
    assert edit_btn is not None, "Edit button not found on first filter row"
    await edit_btn.click()
    await page.wait_for_selector(selectors.FILTER_MODAL_NAME, timeout=10000)
    yield page
    # Teardown: close the modal
    close_btn = await page.query_selector(selectors.FILTER_MODAL_CLOSE)
    if close_btn:
        await close_btn.click()
        await page.wait_for_timeout(1000)


@pytest.mark.asyncio
async def test_edit_button_aria_label(first_filter_row):
    """Edit button has aria-label containing 'Edit filter'."""
    edit_btn = await first_filter_row.query_selector(selectors.FILTER_EDIT_BUTTON)
    assert edit_btn is not None, "Edit button not found"
    aria = await edit_btn.get_attribute("aria-label")
    assert aria and "Edit filter" in aria, f"Expected 'Edit filter' in aria-label, got '{aria}'"


@pytest.mark.asyncio
async def test_filter_toggle_elements(first_filter_row):
    """Toggle checkbox + label present."""
    toggle = await first_filter_row.query_selector(selectors.FILTER_TOGGLE)
    assert toggle is not None, f"Selector {selectors.FILTER_TOGGLE} not found"
    label = await first_filter_row.query_selector(selectors.FILTER_TOGGLE_LABEL)
    assert label is not None, f"Selector {selectors.FILTER_TOGGLE_LABEL} not found"


@pytest.mark.asyncio
async def test_wizard_name_step(open_wizard):
    """Opening edit shows name input + next button."""
    page = open_wizard
    name_input = await page.query_selector(selectors.FILTER_MODAL_NAME)
    assert name_input is not None, f"Selector {selectors.FILTER_MODAL_NAME} not found"
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    assert next_btn is not None, f"Selector {selectors.FILTER_MODAL_NEXT} not found"


@pytest.mark.asyncio
async def test_wizard_conditions_step(open_wizard):
    """Next goes to conditions with button.select dropdowns."""
    page = open_wizard
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    await next_btn.click()
    await page.wait_for_timeout(1000)
    selects = await page.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
    assert len(selects) >= 2, f"Expected >=2 select buttons on conditions step, got {len(selects)}"


@pytest.mark.asyncio
async def test_condition_type_label(open_wizard):
    """First select has an aria-label (sender/recipient/subject/attachment)."""
    page = open_wizard
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    await next_btn.click()
    await page.wait_for_timeout(1000)
    selects = await page.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
    assert len(selects) >= 1, "No select buttons found"
    aria = await selects[0].get_attribute("aria-label")
    assert aria, "First select button has no aria-label"


@pytest.mark.asyncio
async def test_condition_operator_label(open_wizard):
    """Second select has an aria-label."""
    page = open_wizard
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    await next_btn.click()
    await page.wait_for_timeout(1000)
    selects = await page.query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
    assert len(selects) >= 2, f"Expected >=2 select buttons, got {len(selects)}"
    aria = await selects[1].get_attribute("aria-label")
    assert aria, "Second select button has no aria-label"


@pytest.mark.asyncio
async def test_wizard_actions_step(open_wizard):
    """Next twice goes to actions with folder row + mark-as row."""
    page = open_wizard
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    await next_btn.click()
    await page.wait_for_timeout(1000)
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    await next_btn.click()
    await page.wait_for_timeout(1000)
    folder_row = await page.query_selector(selectors.FILTER_ACTION_FOLDER_ROW)
    assert folder_row is not None, f"Selector {selectors.FILTER_ACTION_FOLDER_ROW} not found"
    mark_row = await page.query_selector(selectors.FILTER_ACTION_MARK_AS_ROW)
    assert mark_row is not None, f"Selector {selectors.FILTER_ACTION_MARK_AS_ROW} not found"


@pytest.mark.asyncio
async def test_folder_dropdown_items(open_wizard):
    """Clicking folder select shows li.dropdown-item elements."""
    page = open_wizard
    # Navigate to actions step
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    await next_btn.click()
    await page.wait_for_timeout(1000)
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    await next_btn.click()
    await page.wait_for_timeout(1000)
    # Click folder select
    folder_btn = await page.query_selector(selectors.FOLDER_SELECT)
    assert folder_btn is not None, f"Selector {selectors.FOLDER_SELECT} not found"
    await folder_btn.click()
    await page.wait_for_timeout(500)
    items = await page.query_selector_all(selectors.DROPDOWN_ITEM)
    assert len(items) > 0, f"No {selectors.DROPDOWN_ITEM} elements found after clicking folder select"
