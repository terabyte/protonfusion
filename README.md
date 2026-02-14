# ProtonFusion

![CI](https://github.com/terabyte/protonfusion/actions/workflows/ci.yml/badge.svg)

A safe, reversible tool for consolidating your ProtonMail filters into optimized Sieve scripts. If you have dozens (or hundreds) of filters cluttering your ProtonMail settings, ProtonFusion merges them into clean, efficient Sieve rules.

## Disclaimer

ProtonFusion is an independent, community-developed project. It is not affiliated with, endorsed by, sponsored by, or supported by Proton AG or any of its subsidiaries. "Proton" and "ProtonMail" are trademarks of Proton AG. Use of these names is for descriptive purposes only and does not imply any official connection or authorization.

## What It Does

ProtonMail lets you create filters one at a time through the UI. Over time, you might end up with hundreds of filters that do similar things. ProtonFusion:

1. **Scrapes** your existing filters from ProtonMail's settings UI using browser automation
2. **Backs them up** to timestamped JSON files (with checksums for integrity)
3. **Consolidates** redundant filters into optimized Sieve rules (e.g., 50 "delete spam from X" filters become 1 Sieve rule)
4. **Generates** a clean Sieve script you can upload to ProtonMail
5. **Syncs** the Sieve script to your account and disables the old UI filters (non-destructively)

**Before** (3 separate UI filters):
```
Filter 1: from = "alice@company.com" -> move to "Work"
Filter 2: from = "bob@company.com"   -> move to "Work"
Filter 3: from = "charlie@company.com" -> move to "Work"
```

**After** (1 Sieve rule):
```sieve
require "fileinto";

if address :is "from" ["alice@company.com", "bob@company.com", "charlie@company.com"] {
    fileinto "INBOX.Work";
}
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. See what you've got (read-only)

```bash
# Opens a browser, reads your filters, and displays them - no changes made
python -m src.main show
```

### 3. Back up your filters

```bash
# Same as show, but saves to a timestamped snapshot directory
python -m src.main backup
```

Each backup creates a snapshot at `snapshots/<timestamp>/backup.json`.

### 4. Analyze consolidation opportunities

```bash
python -m src.main analyze --backup latest
```

### 5. Generate consolidated Sieve script

```bash
# Writes consolidated.sieve + manifest.json into the snapshot directory
python -m src.main consolidate --backup latest
```

### 6. Review and upload

Review the generated Sieve script in the snapshot dir, then:

```bash
# Auto-discovers the .sieve file from the snapshot
python -m src.main sync --backup latest
```

## Setting Up a Test Account for Development

If you want to develop or test ProtonFusion, you should use a **dedicated test account** rather than your real ProtonMail account.

### Create a free ProtonMail account

1. Go to [https://account.proton.me/signup](https://account.proton.me/signup)
2. Choose the **Free** plan
3. Pick a username (e.g., `mytestaccount`) and password
4. Complete the signup (you may need to verify via CAPTCHA or secondary email)
5. **Do not enable 2FA** on the test account - automated/headless login doesn't support it

### Set up credentials for automated testing

Create a `.credentials` file in the project root:

```
Username: mytestaccount
Password: mypassword123
```

This file is in `.gitignore` and will never be committed. The automated test workflow reads from this file to log in headlessly.

### Run the tests

Tests are organized into three tiers:

| Tier | Marker | Needs |
|------|--------|-------|
| Unit | _(none)_ | Nothing |
| Integration | `@pytest.mark.integration` | Playwright + Chromium |
| E2E | `@pytest.mark.e2e` | Internet + credentials |

```bash
# Unit + integration (default — e2e excluded automatically)
pytest -v

# Unit only (fast, no browser needed)
pytest -m "not integration" -v

# E2E only (against live ProtonMail)
pytest -m e2e --credentials-file .credentials -v

# Everything (unit + integration + e2e)
pytest -m "" --credentials-file .credentials -v
```

The E2E smoke tests verify that ProtonMail's UI selectors and text strings haven't changed. They log in once per session and check page structure and filter wizard elements.

**Note:** Free ProtonMail accounts are limited to 1 custom filter at a time.

### Continuous Integration

A GitHub Actions workflow runs unit + integration tests on every push and pull request to `main`. E2E tests are excluded automatically (no secrets needed). See `.github/workflows/ci.yml`.

## Commands

| Command | Description |
|---------|-------------|
| `show` | Read and display your current filters (read-only, no changes) |
| `show-backup` | Display filters from a backup file (offline, no login) |
| `backup` | Scrape current filters and save to a timestamped backup |
| `list-snapshots` | Show all available snapshots with statistics |
| `analyze` | View filter statistics and consolidation opportunities |
| `consolidate` | Generate optimized Sieve script from a backup |
| `diff` | Compare two backups or a backup vs current state |
| `sync` | Upload Sieve script and disable old UI filters |
| `sync --show-diff-only` | Preview Sieve changes against the live script (no upload) |
| `restore` | Restore filters to a previous backup state |
| `cleanup` | Delete disabled filters (with confirmation) |

All commands that interact with ProtonMail accept these flags:

- `--headless` - Run browser without a visible window
- `--credentials-file .credentials` - Use stored credentials instead of manual login
- `--manual-login` - Force manual login even if credentials file exists
- `--workers N` / `-w N` - Number of parallel browser tabs for scraping (default: 5, max: 10). Use `-w 1` for sequential scraping.

### Examples

```bash
# Automated backup (5 parallel tabs by default)
python -m src.main backup --headless --credentials-file .credentials

# Faster scraping with 10 parallel tabs
python -m src.main backup --headless --credentials-file .credentials -w 10

# Sequential scraping (one filter at a time)
python -m src.main backup --headless --credentials-file .credentials -w 1

# List all snapshots
python -m src.main list-snapshots

# Analyze a specific backup
python -m src.main analyze --backup latest

# Generate Sieve script (saved into the snapshot directory)
python -m src.main consolidate --backup latest

# Generate Sieve script to a custom path
python -m src.main consolidate --backup latest --output filters.sieve

# Preview what sync would change (auto-discovers .sieve from snapshot)
python -m src.main sync --backup latest --dry-run

# Sync with a specific Sieve file
python -m src.main sync --sieve filters.sieve --backup latest

# Compare two backups
python -m src.main diff --backup1 2026-02-08_19-30-45 --backup2 2026-02-09_10-00-00

# Restore to a previous state
python -m src.main restore --backup 2026-02-08_19-30-45 --headless --credentials-file .credentials
```

## Safety Design

ProtonFusion is designed to be non-destructive:

- **Backup first**: Every operation starts from a backup. Your original filter state is always preserved.
- **Disable, don't delete**: When syncing, old UI filters are disabled (not deleted). You can re-enable them anytime.
- **Dry-run mode**: Preview what `sync` and `cleanup` will do before committing.
- **Checksums**: Backups include SHA256 checksums to detect corruption.
- **Restore**: One command to roll back to any previous backup.

## Architecture

```
snapshots/                     # All data lives here (gitignored)
  2026-02-11_08-16-55/         # One directory per run
    backup.json                # Filter backup with checksums
    consolidated.sieve         # Generated Sieve script
    manifest.json              # Sync state (synced_at: null → ISO timestamp)
  latest -> 2026-02-11_08-16-55  # Symlink to newest snapshot

src/
  main.py              # CLI entry point (Typer)
  models/              # Pydantic data models for filters and backups
  scraper/             # Playwright browser automation
    selectors.py       # CSS selectors for ProtonMail UI elements
    protonmail_scraper.py  # Read-only scraping of filters
    protonmail_sync.py     # Write operations (create/delete filters, upload Sieve)
  parser/              # Convert scraped HTML data into models
  backup/              # Backup management, diffing, restore logic
  consolidator/        # Filter optimization engine
    strategies/        # Pluggable consolidation strategies
  generator/           # Sieve script generation
  utils/               # Config, credentials, constants
```

Set the `PROTONFUSION_DATA_DIR` environment variable to override the snapshot directory (used by E2E tests for isolation).

### Parallel Scraping

Scraping filters is slow because each one requires opening an edit modal and clicking through wizard steps (~5 seconds per filter). With 250 filters, sequential scraping takes ~21 minutes.

To speed this up, ProtonFusion opens multiple browser tabs within the same session (shared cookies) and scrapes filters in parallel. Each worker tab navigates to the filters page independently, scrapes its assigned range of filters, and results are merged by index to preserve priority ordering.

| Filters | Workers | Est. Time | Speedup |
|---------|---------|-----------|---------|
| 250 | 1 | ~21 min | 1x |
| 250 | 3 | ~7 min | ~3x |
| 250 | 5 | ~4 min | ~5x |

Only scraping (read-only) is parallelized. Sync operations (disabling filters, uploading Sieve) remain sequential because they mutate shared server-side state.

### When ProtonMail Changes Their UI

ProtonMail occasionally updates their web UI, which can break the Playwright selectors. When this happens:

1. Open ProtonMail settings manually: Settings gear -> All settings -> Filters
2. Use browser dev tools to inspect the new element structure
3. Update the selectors in `src/scraper/selectors.py`
4. Run the E2E smoke tests: `pytest -m e2e --credentials-file .credentials -v`

All UI selectors are centralized in `selectors.py` to make this straightforward.

## Troubleshooting

**"Login failed"** - Check that your `.credentials` file has the right format (`Username: ...` / `Password: ...`). If your account has 2FA, use `--manual-login` instead.

**"Playwright browser not found"** - Run `playwright install chromium`.

**E2E test hangs** - ProtonMail may be slow to load. The tool uses a 60-second page load timeout. Try running with `--headless` flag to reduce resource usage.

**Selectors not working** - ProtonMail likely updated their UI. See "When ProtonMail Changes Their UI" above.

## License

This project is released into the public domain under the [Unlicense](UNLICENSE). See the [UNLICENSE](UNLICENSE) file for details.
