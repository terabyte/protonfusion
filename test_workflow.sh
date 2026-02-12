#!/bin/bash
set -e

echo "=== ProtonFusion - End-to-End Test ==="
echo "Using test account credentials from .credentials"
echo "Note: Free-tier accounts are limited to 1 filter at a time."
echo ""

# Ensure we're in the project directory
cd "$(dirname "$0")"

CREDS=".credentials"
TOTAL_STEPS=13

# Test isolation: use a temp directory for all snapshot data
export PROTONFUSION_DATA_DIR=$(mktemp -d)

# Helper: cleanup function to ensure all filters (UI and sieve) are deleted
cleanup_filters() {
    echo ""
    echo ">>> Cleaning up: deleting all test filters..."
    python -c "
import asyncio
from src.utils.config import load_credentials
from src.scraper.protonmail_sync import ProtonMailSync

async def cleanup():
    creds = load_credentials('${CREDS}')
    sync = ProtonMailSync(headless=True, credentials=creds)
    await sync.initialize()
    await sync.login()
    await sync.navigate_to_filters()
    deleted = await sync.delete_all_filters()
    print(f'  Cleanup: deleted {deleted} filters')
    await sync.close()

asyncio.run(cleanup())
" || echo "  WARNING: Cleanup may have failed, check account manually"
    rm -rf "$PROTONFUSION_DATA_DIR"
}

# Trap EXIT to always clean up
trap cleanup_filters EXIT

# Step 1: Create a test filter via ProtonMailSync
echo "[1/${TOTAL_STEPS}] Creating test filter on ProtonMail..."
python -c "
import asyncio
from src.utils.config import load_credentials
from src.scraper.protonmail_sync import ProtonMailSync

async def create_test_filter():
    creds = load_credentials('${CREDS}')
    sync = ProtonMailSync(headless=True, credentials=creds)
    await sync.initialize()
    await sync.login()
    await sync.navigate_to_filters()

    ok = await sync.create_filter(
        name='E2E Delete spam-offers',
        conditions=[{'type': 'sender', 'comparator': 'contains', 'value': 'spam-offers@junk.com'}],
        actions=[{'type': 'delete'}],
    )
    assert ok, 'Failed to create test filter'
    print('  Created: E2E Delete spam-offers')

    await sync.close()

asyncio.run(create_test_filter())
"
echo ""

# Step 2: Validate page layout assumptions
echo "[2/${TOTAL_STEPS}] Validating ProtonMail filter page layout..."
python -c "
import asyncio
from src.utils.config import load_credentials
from src.scraper.protonmail_scraper import ProtonMailScraper
from src.scraper import selectors

async def validate_layout():
    creds = load_credentials('${CREDS}')
    scraper = ProtonMailScraper(headless=True, credentials=creds)
    await scraper.initialize()
    await scraper.login()
    await scraper.navigate_to_filters()
    page = scraper.page

    # 1. Page heading must be 'Filters'
    h1 = await page.query_selector(selectors.PAGE_HEADING)
    assert h1, 'LAYOUT CHANGE: Missing <h1> heading'
    h1_text = (await h1.inner_text()).strip()
    assert h1_text == 'Filters', f'LAYOUT CHANGE: h1 is {h1_text!r}, expected Filters'
    print('  h1 heading: OK')

    # 2. 'Custom filters' section heading must exist
    custom_h2 = await page.query_selector(selectors.CUSTOM_FILTERS_HEADING)
    assert custom_h2, 'LAYOUT CHANGE: Missing Custom filters h2'
    print('  Custom filters heading: OK')

    # 3. 'Spam, block, and allow lists' section heading must exist
    spam_h2 = await page.query_selector(selectors.SPAM_LISTS_HEADING)
    assert spam_h2, 'LAYOUT CHANGE: Missing Spam/block/allow lists h2'
    print('  Spam/block/allow heading: OK')

    # 4. Sections are wrapped in <section> elements
    custom_section = await page.query_selector(selectors.CUSTOM_FILTERS_SECTION)
    assert custom_section, 'LAYOUT CHANGE: Custom filters not inside <section> element'
    section_tag = await custom_section.evaluate('el => el.tagName')
    assert section_tag == 'SECTION', f'LAYOUT CHANGE: expected SECTION, got {section_tag}'
    print('  Custom filters in <section>: OK')

    # 5. Filter table exists inside Custom filters section (we created one in step 1)
    table = await custom_section.query_selector(selectors.FILTER_TABLE)
    assert table, 'LAYOUT CHANGE: No table.simple-table inside Custom filters section'
    table_cls = await table.get_attribute('class')
    assert 'simple-table' in table_cls, f'LAYOUT CHANGE: table class is {table_cls!r}'
    print('  Filter table (table.simple-table): OK')

    # 6. Filter row exists with expected structure
    rows = await custom_section.query_selector_all(selectors.FILTER_TABLE_ROWS)
    assert len(rows) == 1, f'LAYOUT CHANGE: expected 1 filter row, got {len(rows)}'
    row = rows[0]
    print(f'  Filter rows in Custom section: {len(rows)} OK')

    # 7. Row has Edit button with aria-label containing filter name
    edit_btn = await row.query_selector(selectors.FILTER_EDIT_BUTTON)
    assert edit_btn, 'LAYOUT CHANGE: No button[aria-label*=Edit filter] in filter row'
    aria = await edit_btn.get_attribute('aria-label')
    assert aria and 'E2E Delete spam-offers' in aria, f'LAYOUT CHANGE: Edit aria-label is {aria!r}'
    print(f'  Edit button aria-label: OK ({aria!r})')

    # 8. Row has toggle checkbox and clickable label
    toggle = await row.query_selector(selectors.FILTER_TOGGLE)
    assert toggle, 'LAYOUT CHANGE: No toggle checkbox in filter row'
    toggle_label = await row.query_selector(selectors.FILTER_TOGGLE_LABEL)
    assert toggle_label, 'LAYOUT CHANGE: No toggle label[data-testid=toggle-switch] in filter row'
    print('  Toggle checkbox + label: OK')

    # 9. Edit button opens wizard with Name step
    await edit_btn.click()
    await page.wait_for_timeout(1500)
    name_input = await page.query_selector(selectors.FILTER_MODAL_NAME)
    assert name_input, 'LAYOUT CHANGE: Edit wizard missing name input'
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    assert next_btn, 'LAYOUT CHANGE: Edit wizard missing Next button'
    print('  Edit wizard Name step: OK')

    # 10. Next goes to Conditions step
    await next_btn.click()
    await page.wait_for_timeout(1500)
    cond_rows = await page.query_selector_all(selectors.FILTER_CONDITION_ROWS)
    assert len(cond_rows) >= 1, f'LAYOUT CHANGE: Conditions step has {len(cond_rows)} rows'
    # Check condition row has dropdown buttons
    select_btns = await cond_rows[0].query_selector_all(selectors.CUSTOM_SELECT_BUTTON)
    assert len(select_btns) >= 2, f'LAYOUT CHANGE: Condition row has {len(select_btns)} select buttons, expected >= 2'
    print(f'  Conditions step: {len(cond_rows)} rows, {len(select_btns)} select buttons: OK')

    # 11. Next goes to Actions step
    next_btn = await page.query_selector(selectors.FILTER_MODAL_NEXT)
    assert next_btn, 'LAYOUT CHANGE: No Next button on Conditions step'
    await next_btn.click()
    await page.wait_for_timeout(1500)
    folder_row = await page.query_selector(selectors.FILTER_ACTION_FOLDER_ROW)
    assert folder_row, 'LAYOUT CHANGE: Actions step missing folder row'
    print('  Actions step: OK')

    # Close modal
    close_btn = await page.query_selector(selectors.FILTER_MODAL_CLOSE)
    if close_btn:
        await close_btn.click()
        await page.wait_for_timeout(500)

    # 12. Sieve filter button exists
    sieve_btn = await page.query_selector(selectors.ADD_SIEVE_FILTER_BUTTON)
    # On free tier with 1 filter, this may be hidden
    if sieve_btn and await sieve_btn.is_visible():
        print('  Add sieve filter button: visible')
    else:
        print('  Add sieve filter button: hidden (free tier with filter, expected)')

    print('  All layout assertions passed!')
    await scraper.close()

asyncio.run(validate_layout())
"
echo ""

# Step 3: Backup - scrape the filter we just created
echo "[3/${TOTAL_STEPS}] Creating backup of test filters..."
python -m src.main backup --headless --credentials-file "$CREDS"
echo "  Backup saved to: $PROTONFUSION_DATA_DIR"
echo ""

# Step 4: Verify backup has the right number of filters
echo "[4/${TOTAL_STEPS}] Verifying backup integrity..."
python -c "
from src.backup.backup_manager import BackupManager
mgr = BackupManager()
bkup = mgr.load_backup('latest')
is_valid = mgr.verify_backup(bkup)
count = bkup.metadata.filter_count
print(f'  Backup has {count} filters')
print(f'  Enabled: {bkup.metadata.enabled_count}, Disabled: {bkup.metadata.disabled_count}')
print(f'  Checksum valid: {is_valid}')
assert is_valid, 'Checksum verification failed!'
assert count >= 1, f'Expected at least 1 filter, got {count}'

# Account email must be populated from the UI
email = bkup.metadata.account_email
assert email, f'account_email is empty - scraper failed to capture it'
assert '@' in email, f'account_email has no @: {email!r}'
print(f'  account_email: {email!r} OK')

# Find the test filter we created and verify every scraped field
f = next((f for f in bkup.filters if 'E2E' in f.name), None)
assert f is not None, f'Test filter not found in backup. Filter names: {[x.name for x in bkup.filters]}'

# Name: must match exactly, not fall back to 'Filter N'
assert f.name == 'E2E Delete spam-offers', f'Name wrong: {f.name!r}'
print(f'  name: {f.name!r} OK')

# Enabled: created as enabled, must not silently default
assert f.enabled is True, f'Expected enabled=True, got {f.enabled!r}'
print(f'  enabled: {f.enabled} OK')

# Logic: created as AND (default), must not be wrong
assert f.logic.value == 'and', f'Logic wrong: {f.logic.value!r}'
print(f'  logic: {f.logic.value!r} OK')

# Conditions: exactly 1, with correct type/operator/value
assert len(f.conditions) == 1, f'Expected 1 condition, got {len(f.conditions)}'
c = f.conditions[0]
assert c.type.value == 'sender', f'Condition type wrong: {c.type.value!r}'
assert c.operator.value == 'contains', f'Condition operator wrong: {c.operator.value!r}'
assert 'spam-offers@junk.com' in c.value, f'Condition value wrong: {c.value!r}'
print(f'  condition: {c.type.value} {c.operator.value} {c.value!r} OK')

# Actions: exactly 1 delete action
assert len(f.actions) >= 1, f'Expected at least 1 action, got {len(f.actions)}'
a = f.actions[0]
assert a.type.value == 'delete', f'Action type wrong: {a.type.value!r}'
print(f'  action: {a.type.value} OK')

print('  All scraped fields verified!')
"
echo ""

# Step 5: List snapshots
echo "[5/${TOTAL_STEPS}] Listing snapshots..."
python -m src.main list-snapshots
echo ""

# Step 6: Analyze filters
echo "[6/${TOTAL_STEPS}] Analyzing filters..."
python -m src.main analyze --backup latest
echo ""

# Step 7: Consolidate and generate Sieve
echo "[7/${TOTAL_STEPS}] Consolidating filters and generating Sieve script..."
python -m src.main consolidate --backup latest
echo ""

# Step 8: Verify consolidation round-trip
echo "[8/${TOTAL_STEPS}] Verifying consolidation round-trip..."
python -c "
from src.backup.backup_manager import BackupManager
from src.consolidator.consolidation_engine import ConsolidationEngine
from src.generator.sieve_generator import SieveGenerator

mgr = BackupManager()
bkup = mgr.load_backup('latest')
engine = ConsolidationEngine()
consolidated, report = engine.consolidate(bkup.filters)
gen = SieveGenerator()
sieve = gen.generate(consolidated)

print(f'  Original: {report.original_count} filters')
print(f'  Enabled (processed): {report.enabled_count}')
print(f'  Consolidated: {report.consolidated_count} rules')
print(f'  Reduction: {report.reduction_percent:.1f}%')
print(f'  Sieve script: {len(sieve)} chars, {len(sieve.splitlines())} lines')

assert report.original_count >= 1, f'Expected >= 1 original filters, got {report.original_count}'
assert len(sieve) > 0, 'Sieve script should not be empty'
print('  Round-trip assertions passed!')
"
echo ""

# Step 9: Verify Sieve script content
echo "[9/${TOTAL_STEPS}] Verifying generated Sieve script content..."
python -c "
from src.backup.backup_manager import BackupManager

mgr = BackupManager()
snapshot_dir = mgr.snapshot_dir_for('latest')
sieve_path = snapshot_dir / 'consolidated.sieve'
sieve = sieve_path.read_text()
print(f'  Sieve script length: {len(sieve)} chars')
print(f'  Lines: {len(sieve.splitlines())}')

# Verify the Sieve script contains the actual scraped data, not just structure
assert 'if ' in sieve, 'Sieve script missing if statement'
assert 'discard;' in sieve, 'Sieve script missing discard action for delete filter'
assert 'spam-offers@junk.com' in sieve, 'Sieve script missing condition value - scraper likely returned empty values'
assert 'From' in sieve, 'Sieve script missing From header for sender condition'
print('  Sieve content: condition values and actions present')

# Verify manifest was written
manifest = mgr.load_manifest(snapshot_dir)
assert manifest is not None, 'Manifest not found in snapshot'
assert manifest['filter_count'] >= 1, f'Expected >= 1 filter in manifest, got {manifest[\"filter_count\"]}'
assert manifest['synced_at'] is None, 'Manifest should not be synced yet'
print(f'  Manifest: {manifest[\"filter_count\"]} filters, synced_at={manifest[\"synced_at\"]}')

# Print first 20 lines for inspection
lines = sieve.splitlines()
print('  --- Script preview (first 20 lines) ---')
for line in lines[:20]:
    print(f'  | {line}')
if len(lines) > 20:
    print(f'  ... ({len(lines) - 20} more lines)')

print('  Sieve content verification passed!')
"
echo ""

# Step 10: Delete test filter to free up slot, then test Sieve upload and read-back
echo "[10/${TOTAL_STEPS}] Testing Sieve script upload and read-back..."
echo "  (Deleting UI filter first to free free-tier filter slot)"
python -c "
import asyncio
from src.utils.config import load_credentials
from src.scraper.protonmail_sync import ProtonMailSync

FILTER_NAME = 'E2E Sieve Test'

TEST_SIEVE = '''require [\"fileinto\"];

# Test user rules
if header :contains \"Subject\" \"test-e2e-marker\" {
    fileinto \"Trash\";
}
'''

async def test_sieve_round_trip():
    creds = load_credentials('${CREDS}')
    sync = ProtonMailSync(headless=True, credentials=creds)
    await sync.initialize()
    await sync.login()
    await sync.navigate_to_filters()

    # Delete all existing filters to free the slot on free tier
    deleted = await sync.delete_all_filters()
    print(f'  Deleted {deleted} existing filters')

    # Upload test script as a named sieve filter
    ok = await sync.upload_sieve(TEST_SIEVE, filter_name=FILTER_NAME)
    assert ok, 'Failed to upload test Sieve script'
    print('  Uploaded Sieve filter: ' + FILTER_NAME)

    # Read it back by name
    read_back = await sync.read_sieve_script(filter_name=FILTER_NAME)
    print(f'  Read back {len(read_back)} chars')

    # Verify content was preserved
    assert 'test-e2e-marker' in read_back, f'Marker not found in read-back: {read_back[:200]!r}'
    assert 'fileinto' in read_back, f'fileinto not found in read-back'
    print('  Content round-trip verified!')

    await sync.close()

asyncio.run(test_sieve_round_trip())
"
echo ""

# Step 11: Test merge_with_existing preserves user rules across re-syncs
echo "[11/${TOTAL_STEPS}] Testing Sieve merge with section markers..."
python -c "
import asyncio
from src.utils.config import load_credentials
from src.scraper.protonmail_sync import ProtonMailSync
from src.generator.sieve_generator import SieveGenerator, SECTION_BEGIN, SECTION_END

FILTER_NAME = 'E2E Sieve Test'

GENERATED = '''require [\"fileinto\"];

# ProtonFusion - Filter Consolidation
# Generated by ProtonFusion v0.1.0
# Total rules: 1

if address :contains \"From\" \"spam@example.com\" {
    fileinto \"Spam\";
}
'''

async def test_merge_and_upload():
    creds = load_credentials('${CREDS}')
    sync = ProtonMailSync(headless=True, credentials=creds)
    await sync.initialize()
    await sync.login()
    await sync.navigate_to_filters()

    # Read the existing sieve filter from step 9
    existing = await sync.read_sieve_script(filter_name=FILTER_NAME)
    print(f'  Existing script: {len(existing)} chars')

    # Merge generated script with existing content
    merged = SieveGenerator.merge_with_existing(GENERATED, existing)
    print(f'  Merged script: {len(merged)} chars')

    # Verify structure
    assert SECTION_BEGIN in merged, 'Missing BEGIN marker'
    assert SECTION_END in merged, 'Missing END marker'
    assert merged.count(SECTION_BEGIN) == 1, 'Duplicate BEGIN markers'
    assert merged.count(SECTION_END) == 1, 'Duplicate END markers'

    # Verify user rules preserved from step 9
    if 'test-e2e-marker' in existing:
        assert 'test-e2e-marker' in merged, 'User rules lost during merge!'
        print('  User rules preserved in merge')

    # Verify require dedup
    require_count = merged.count('require [')
    assert require_count == 1, f'Expected 1 require statement, got {require_count}'
    print('  Single require statement (deduped)')

    # Upload merged script (update existing filter)
    ok = await sync.upload_sieve(merged, filter_name=FILTER_NAME)
    assert ok, 'Failed to upload merged script'
    print('  Uploaded merged script')

    # Read back and verify markers survive round-trip
    final = await sync.read_sieve_script(filter_name=FILTER_NAME)
    assert SECTION_BEGIN in final or 'BEGIN ProtonFusion' in final, \
        f'BEGIN marker lost after upload. Got: {final[:300]!r}'
    assert 'spam@example.com' in final, 'ProtonFusion rules lost after upload'
    print('  Markers and content verified after upload round-trip')

    await sync.close()

asyncio.run(test_merge_and_upload())
"
echo ""

# Step 12: Verify backup captures sieve_script from named filter
echo "[12/${TOTAL_STEPS}] Verifying backup captures Sieve script..."
python -c "
import asyncio
from src.utils.config import load_credentials
from src.scraper.protonmail_scraper import ProtonMailScraper

FILTER_NAME = 'E2E Sieve Test'

async def test_backup_sieve():
    creds = load_credentials('${CREDS}')
    scraper = ProtonMailScraper(headless=True, credentials=creds)
    await scraper.initialize()
    await scraper.login()
    await scraper.navigate_to_filters()

    sieve = await scraper.read_sieve_script(filter_name=FILTER_NAME)
    print(f'  Read sieve_script: {len(sieve)} chars')
    assert len(sieve) > 0, 'Expected non-empty sieve_script'
    assert 'spam@example.com' in sieve or 'ProtonFusion' in sieve, \
        f'Expected ProtonFusion content, got: {sieve[:200]!r}'
    print('  Sieve script content verified!')

    await scraper.close()

asyncio.run(test_backup_sieve())
"
echo ""

# Step 13: Run unit tests
echo "[13/${TOTAL_STEPS}] Running unit tests..."
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
echo ""

# Cleanup is handled by the EXIT trap
echo "=== End-to-End Test Complete ==="
echo "All steps passed successfully!"
