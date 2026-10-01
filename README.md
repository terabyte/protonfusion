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

### 2. Sign in once

```bash
# Opens a visible browser; sign in (CAPTCHA, 2FA) and the session is saved
python -m src.main login
```

Proton puts a Human Verification CAPTCHA in front of automated logins, so sign in by hand once. The session is saved to `~/.config/protonfusion/storage_state.json` (owner-only, it holds live auth cookies) and every other command reuses it, headless included, until Proton expires it. Then run `login` again.

### 3. See what you've got (read-only)

```bash
# Reads your filters and displays them - no changes made
python -m src.main show
```

### 4. Back up your filters

```bash
# Same as show, but saves to a timestamped snapshot directory
python -m src.main backup
```

Each backup creates a snapshot at `snapshots/<timestamp>/backup.json`.

If any filter could not be fully read (an action it cannot express such as
auto-reply, a row it cannot parse, an unknown condition operator),
`backup` lists each one with the reason and exits with status 1 without
saving. Pass `--allow-incomplete` to save anyway; those filters are flagged in
`backup.json` and `cleanup` will not delete them.

### 5. Analyze consolidation opportunities

```bash
python -m src.main analyze --backup latest
```

### 6. Generate consolidated Sieve script

```bash
# Writes consolidated.sieve + manifest.json into the snapshot directory
python -m src.main consolidate --backup latest
```

### 7. Review and upload

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
| `login` | Sign in by hand in a visible browser and save the session for the other commands |
| `show` | Read and display your current filters (read-only, no changes) |
| `show-backup` | Display filters from a backup file (offline, no login) |
| `backup` | Scrape current filters and save to a timestamped backup |
| `list-snapshots` | Show all available snapshots with statistics |
| `analyze` | View filter statistics and consolidation opportunities |
| `consolidate` | Generate optimized Sieve script from a backup |
| `consolidate --keep-live-rules` | Also carry forward rules that exist only in the live Sieve section (saved to the archive) |
| `diff` | Compare two backups or a backup vs current state |
| `sync` | Upload Sieve script and disable old UI filters; refuses if the new script drops live rules (`--allow-rule-removal` to override) |
| `sync --show-diff-only` | Preview Sieve changes against the live script (no upload) |
| `restore` | Restore filters to a previous backup state |
| `cleanup` | Delete disabled filters whose rules are in the live Sieve section and that have a verified backup copy (with confirmation) |
| `snapshot view` | View merged backup + archive filters grouped by status |
| `snapshot set-status` | Change a filter's lifecycle status (enabled/disabled/archived/deprecated) |
| `snapshot remove` | Remove a filter from the archive |

All commands that interact with ProtonMail accept these flags:

- `--state PATH` - Saved session file from `login` (default: `$PROTONFUSION_STORAGE_STATE`, else `~/.config/protonfusion/storage_state.json`). Used whenever it exists.
- `--headless` - Run browser without a visible window
- `--credentials-file .credentials` - Use stored credentials instead of manual login
- `--manual-login` - Force manual login even if credentials file exists
- `--workers N` / `-w N` - Number of parallel browser tabs for scraping (default: 5, max: 10). Use `-w 1` for sequential scraping.

### Examples

```bash
# Sign in by hand once (pre-fill the form from .credentials, wait up to 15 min)
python -m src.main login --credentials-file .credentials --timeout 900

# Headless backup reusing the saved session (5 parallel tabs by default)
python -m src.main backup --headless

# Automated backup with credentials (Proton may block this with a CAPTCHA)
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

# View filters in latest snapshot (backup + archive merged, grouped by status)
python -m src.main snapshot view

# Mark a filter as deprecated (excluded from future consolidations)
python -m src.main snapshot set-status "Old Newsletter Filter" deprecated

# Re-include a deprecated filter
python -m src.main snapshot set-status "Old Newsletter Filter" archived

# Exclude specific filters during consolidation
python -m src.main consolidate --backup latest --exclude "Temp Filter"

# Reuse exclude args from a previous consolidation
python -m src.main consolidate --backup latest --include-args-from 2026-02-11_08-16-55
```

## Safety Design

ProtonFusion is designed to be non-destructive:

- **Backup first**: Every operation starts from a backup. Your original filter state is always preserved.
- **Disable, don't delete**: When syncing, old UI filters are disabled (not deleted). You can re-enable them anytime.
- **Refuse rather than drop**: `sync` refuses to upload a ProtonFusion section that would lose any rule in the live one, and `cleanup` never deletes a filter whose rules are not in the live Sieve script.
- **Dry-run mode**: Preview what `sync` and `cleanup` will do before committing.
- **Checksums**: Backups include SHA256 checksums to detect corruption.
- **Incomplete reads are loud**: A filter the scraper cannot fully read stops `backup` (override: `--allow-incomplete`). Each backed-up filter also keeps the wizard's raw text, so a field the parser missed can still be recovered.
- **Cleanup needs a verified copy**: `cleanup` only deletes a disabled filter if the latest snapshot holds an identical, complete copy with raw text. Others are listed and kept (override: `--allow-incomplete`). Backups made before format 1.1 have no raw text, so run `backup` again after upgrading.
- **Restore**: One command to roll back to any previous backup.

## Architecture

```
snapshots/                     # All data lives here (gitignored)
  2026-02-11_08-16-55/         # One directory per run
    backup.json                # Filter backup with checksums (immutable)
    archive.json               # Filters carried forward from prior consolidations
    consolidated.sieve         # Generated Sieve script
    manifest.json              # Sync state (synced_at: null → ISO timestamp)
    consolidation_args.json    # Saved --exclude args for reuse
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

### Filter Lifecycle

Filters have four possible statuses that control how they're handled:

| Status | In Sieve? | On ProtonMail? | Purpose |
|--------|-----------|----------------|---------|
| `enabled` | Yes | Yes (active) | Live UI filter |
| `disabled` | Yes | Yes (inactive) | Live UI filter (turned off) |
| `archived` | Yes | No | Preserved in Sieve after cleanup |
| `deprecated` | No | No | Excluded from everything |

The typical workflow:

```
backup → consolidate → sync → cleanup → backup → consolidate → ...
```

1. `backup` scrapes live filters into `backup.json` and copies `archive.json` from the previous snapshot
2. `consolidate` reads both files, generates Sieve, and auto-archives included backup filters
3. `sync` uploads the Sieve script and disables UI filters
4. `cleanup` deletes disabled UI filters from ProtonMail, refusing any without a complete backup copy
5. Next `backup` scrapes the now-reduced filter list; archived filters carry forward via `archive.json`
6. Next `consolidate` still has all rules from the archive

If the live Sieve section holds rules the archive does not (for example rules consolidated before the archive existed), `consolidate` warns and `sync` refuses. Run `consolidate --keep-live-rules` once to rebuild them into the archive.

Use `snapshot set-status <name> deprecated` to permanently exclude a rule, or `snapshot set-status <name> archived` to re-include it.

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

1. Open the filters page manually: `https://account.proton.me/u/<slot>/mail/filters` (or Settings gear -> All settings -> Filters)
2. Use browser dev tools to inspect the new element structure
3. Update the selectors in `src/scraper/selectors.py`
4. Run the E2E smoke tests: `pytest -m e2e --credentials-file .credentials -v`

All UI selectors are centralized in `selectors.py` to make this straightforward.

## Troubleshooting

**"The saved session ... has expired"** - Proton ended the saved session. Run `python -m src.main login` again.

**"Login failed"** - Check that your `.credentials` file has the right format (`Username: ...` / `Password: ...`). If Proton shows Human Verification (CAPTCHA) or your account has 2FA, use `python -m src.main login` to sign in by hand and save a session.

**"Playwright browser not found"** - Run `playwright install chromium`.

**E2E test hangs** - ProtonMail may be slow to load. The tool uses a 60-second page load timeout. Try running with `--headless` flag to reduce resource usage.

**Selectors not working** - ProtonMail likely updated their UI. See "When ProtonMail Changes Their UI" above.

## License

This project is released into the public domain under the [Unlicense](UNLICENSE). See the [UNLICENSE](UNLICENSE) file for details.
