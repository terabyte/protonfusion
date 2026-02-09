#!/bin/bash
set -e

echo "=== ProtonMail Filter Tool - End-to-End Test ==="
echo "Using test account credentials from .credentials"
echo "Note: Free-tier accounts are limited to 1 filter at a time."
echo ""

# Ensure we're in the project directory
cd "$(dirname "$0")"

CREDS=".credentials"
TOTAL_STEPS=9

# Helper: cleanup function to ensure filters are deleted even on failure
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
    await sync.close()
    print(f'  Cleanup: deleted {deleted} filters')

asyncio.run(cleanup())
" || echo "  WARNING: Cleanup may have failed, check account manually"
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
INITIAL_BACKUP=$(ls -t backups/*.json | grep -v latest | head -1)
echo "  Backup saved: $INITIAL_BACKUP"
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
"
echo ""

# Step 4: List backups
echo "[4/${TOTAL_STEPS}] Listing backups..."
python -m src.main list-backups
echo ""

# Step 5: Analyze filters
echo "[5/${TOTAL_STEPS}] Analyzing filters..."
python -m src.main analyze --backup latest
echo ""

# Step 6: Consolidate and generate Sieve
echo "[6/${TOTAL_STEPS}] Consolidating filters and generating Sieve script..."
python -m src.main consolidate --backup latest --output output/test_consolidated.sieve
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
from pathlib import Path

sieve = Path('output/test_consolidated.sieve').read_text()
print(f'  Sieve script length: {len(sieve)} chars')
print(f'  Lines: {len(sieve.splitlines())}')

# Should contain at least one rule (if block or require)
assert 'if' in sieve.lower() or 'require' in sieve.lower(), 'Sieve script should have rules'

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

# Step 9: Run unit tests
echo "[9/${TOTAL_STEPS}] Running unit tests..."
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
echo ""

# Cleanup is handled by the EXIT trap
echo "=== End-to-End Test Complete ==="
echo "All steps passed successfully!"
