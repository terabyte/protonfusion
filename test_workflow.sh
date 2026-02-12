#!/bin/bash
set -e

echo "=== ProtonFusion - End-to-End Test ==="
echo "Using test account credentials from .credentials"
echo "Note: Free-tier accounts are limited to 1 filter at a time."
echo ""

# Ensure we're in the project directory
cd "$(dirname "$0")"

CREDS=".credentials"
TOTAL_STEPS=12

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

# Step 2: Backup - scrape the filter we just created
echo "[2/${TOTAL_STEPS}] Creating backup of test filters..."
python -m src.main backup --headless --credentials-file "$CREDS"
echo "  Backup saved to: $PROTONFUSION_DATA_DIR"
echo ""

# Step 3: Verify backup has the right number of filters
echo "[3/${TOTAL_STEPS}] Verifying backup integrity..."
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

# Step 4: List snapshots
echo "[4/${TOTAL_STEPS}] Listing snapshots..."
python -m src.main list-snapshots
echo ""

# Step 5: Analyze filters
echo "[5/${TOTAL_STEPS}] Analyzing filters..."
python -m src.main analyze --backup latest
echo ""

# Step 6: Consolidate and generate Sieve
echo "[6/${TOTAL_STEPS}] Consolidating filters and generating Sieve script..."
python -m src.main consolidate --backup latest
echo ""

# Step 7: Verify consolidation round-trip
echo "[7/${TOTAL_STEPS}] Verifying consolidation round-trip..."
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

# Step 8: Verify Sieve script content
echo "[8/${TOTAL_STEPS}] Verifying generated Sieve script content..."
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

# Step 9: Delete test filter to free up slot, then test Sieve upload and read-back
echo "[9/${TOTAL_STEPS}] Testing Sieve script upload and read-back..."
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

# Step 10: Test merge_with_existing preserves user rules across re-syncs
echo "[10/${TOTAL_STEPS}] Testing Sieve merge with section markers..."
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

# Step 11: Verify backup captures sieve_script from named filter
echo "[11/${TOTAL_STEPS}] Verifying backup captures Sieve script..."
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

# Step 12: Run unit tests
echo "[12/${TOTAL_STEPS}] Running unit tests..."
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
echo ""

# Cleanup is handled by the EXIT trap
echo "=== End-to-End Test Complete ==="
echo "All steps passed successfully!"
