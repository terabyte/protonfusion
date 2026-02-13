# Testing

ProtonFusion has two testing layers: a comprehensive unit test suite that runs offline, and an end-to-end test that exercises the full workflow against a live ProtonMail account.

## Unit Tests

258 tests across 7 test files, plus a shared `conftest.py` with 18 fixtures.

### Running

```bash
# All unit tests
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_consolidator.py -v

# Specific test
python -m pytest tests/test_sieve_generator.py::test_generate_basic_rule -v
```

### Test Files

| File | What It Tests |
|------|--------------|
| `test_models.py` | Pydantic model validation, content hashing, serialization round-trips |
| `test_parser.py` | Condition/operator/action type mapping from scraped data |
| `test_backup.py` | Backup creation, loading, listing, checksum verification, manifests |
| `test_consolidator.py` | All three consolidation strategies and the engine pipeline |
| `test_sieve_generator.py` | Sieve script generation, extension collection, merging with existing scripts |
| `test_diff.py` | Filter comparison (added, removed, modified, state_changed, unchanged) |
| `test_config.py` | Configuration loading, credential parsing |
| `test_scraper.py` | Selector validation (offline, no browser needed) |
| `test_parallel_scraping.py` | Worker distribution logic, chunk assignment |

### Key Fixtures (`conftest.py`)

The shared fixture file provides sample data for consistent test setup:

- **Sample filters**: spam filter, move-to-folder filter, disabled filter, complex multi-condition filter
- **Sample conditions and actions**: pre-built `FilterCondition` and `FilterAction` instances
- **Sample consolidated filters**: pre-built `ConsolidatedFilter` instances with condition groups
- **Sample backups**: complete `Backup` objects with metadata and checksums
- **Temporary directories**: `temp_snapshots_dir` for tests that create/load snapshots, cleaned up automatically

### Testing Notes

- Backup tests that create time-based directory names need `sleep(1)` between creates to avoid timestamp collisions.
- The `temp_snapshots_dir` fixture patches `PROTONFUSION_DATA_DIR` to isolate tests from real snapshot data.

## End-to-End Test

The E2E test (`test_workflow.sh`) runs 13 steps against a live ProtonMail test account.

### Prerequisites

1. A dedicated ProtonMail test account (free tier is fine)
2. Credentials in `.credentials` file (see README)
3. 2FA must be disabled on the test account
4. Playwright browsers installed (`playwright install chromium`)

### Running

```bash
bash test_workflow.sh
```

### Test Steps

1. **Create a test filter** via the UI wizard
2. **Validate page layout** (12 assertions on headings, sections, table structure, edit buttons, wizard steps)
3. **Backup filters** to a snapshot
4. **List snapshots** and verify the new one appears
5. **Show backup** contents offline
6. **Analyze** consolidation opportunities
7. **Consolidate** into a Sieve script
8. **Upload Sieve** and disable UI filters
9. **Read back Sieve** from ProtonMail to verify upload
10. **Merge Sieve** with existing script content
11. **Run unit tests** (full pytest suite)
12. **Restore** filter state from backup
13. **Clean up** test filters

### Test Isolation

The E2E test creates a temporary data directory:

```bash
PROTONFUSION_DATA_DIR=$(mktemp -d)
```

This ensures test snapshots never touch the real `snapshots/` directory. The temp directory is cleaned up on exit.

## ProtonMail Free Tier Constraints

The E2E tests are designed to run against a **free ProtonMail account**. This is a deliberate choice -- requiring a paid account would make the tests inaccessible to most contributors. However, the free tier has significant limitations that shape what the E2E tests can and cannot do.

### The 1-Filter Limit

Free ProtonMail accounts are limited to **1 custom filter at a time**. This is the single biggest constraint on E2E testing. When the limit is reached:

- The "Add filter" button disappears from the UI entirely
- The "Add sieve filter" button also disappears
- Both are replaced by a "Get more filters" upsell link
- There is no way to programmatically bypass this -- the server enforces it

This means the E2E test can never have more than 1 filter on the account at any given moment. Every test step that creates a filter must delete or disable the previous one first.

### Implications for Test Design

Because of the 1-filter limit:

- **No multi-filter consolidation testing in E2E.** The consolidation engine's ability to merge 50 filters into 1 rule can only be tested in unit tests (which use synthetic data and don't touch ProtonMail). The E2E test exercises the consolidation pipeline with a single filter to verify the plumbing works.
- **Sieve filter and UI filter compete for the same slot.** The sync workflow must disable/delete the UI filter before it can create a Sieve filter. The E2E test verifies this sequencing works.
- **Tests must clean up after themselves.** If a test run fails partway through and leaves a filter behind, the next run will fail at the "create filter" step because the slot is occupied. The E2E test has cleanup logic, but manual cleanup may be needed after crashes.
- **Parallel E2E test runs are not possible.** Two test runs against the same account would conflict over the single filter slot.

### Other Free Tier Limitations

- **No custom domains.** Tests use `@proton.me` addresses only.
- **Limited storage.** Not a practical issue for filter testing, but means the test account shouldn't accumulate mail.
- **Rate limiting.** ProtonMail may throttle rapid UI interactions. The scraper uses conservative wait times (1500ms per modal transition) to avoid this.
- **No API access.** Even paid accounts don't have a public filter management API, so this isn't a free-vs-paid distinction -- but it's worth noting that there's no shortcut available at any tier.

### What This Means for Contributors

When writing new E2E test steps:

1. **Assume only 1 filter slot is available.** Always delete/disable existing filters before creating new ones.
2. **Put multi-filter logic in unit tests.** Use the fixtures in `conftest.py` to create synthetic filter lists of any size. The consolidation engine, Sieve generator, diff engine, and backup manager are all fully testable offline.
3. **Keep E2E steps focused on browser integration.** The E2E test's job is to verify that Playwright can still navigate ProtonMail's UI, read/write filters, and upload Sieve scripts. The business logic is covered by unit tests.
4. **Handle cleanup failures gracefully.** If your step creates something on ProtonMail, add cleanup logic and document what manual cleanup looks like if the script is interrupted.

## Writing New Tests

### Unit Tests

Add tests to the appropriate `test_*.py` file. Use fixtures from `conftest.py` for sample data:

```python
def test_my_feature(sample_spam_filter, temp_snapshots_dir):
    # sample_spam_filter is a ProtonMailFilter fixture
    # temp_snapshots_dir is an isolated Path for snapshots
    ...
```

### E2E Tests

Add steps to `test_workflow.sh`. Each step follows the pattern:

```bash
echo "=== Step N: Description ==="
python -m src.main <command> <args> --headless --credentials-file .credentials
if [ $? -ne 0 ]; then
    echo "FAIL: Description"
    exit 1
fi
echo "PASS: Description"
```
