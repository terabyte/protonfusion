# ProtonMail Filter Consolidation Tool

A safe, reversible tool for consolidating your ProtonMail filters into fewer entries using Sieve scripts.

## ⚠️ Safety First

This tool is designed with **safety and reversibility** as top priorities:

- ✅ **Backup before changes**: Always creates a backup before any modifications
- ✅ **Never destructive by default**: Old filters are disabled, not deleted
- ✅ **Preview changes**: See what will change before applying with `diff`
- ✅ **Easy rollback**: Restore to any previous backup with one command
- ✅ **Checksum verification**: Backups include SHA256 checksums to prevent corruption

**⚠️ ALWAYS backup before syncing changes!**

## Features

- 🔍 **Scrape**: Extract filters from ProtonMail UI
- 💾 **Backup**: Save filters to timestamped JSON with state tracking
- 🔀 **Consolidate**: Merge filters into optimized Sieve scripts
- 👁️ **Diff**: Preview changes before applying
- 🔄 **Sync**: Upload Sieve and disable old UI filters (non-destructive)
- ↩️ **Restore**: Rollback to any previous backup
- 🧹 **Cleanup**: Delete disabled filters (optional, separate command)
- 📊 **Analyze**: View filter statistics and consolidation opportunities

## Installation

### Requirements

- Python 3.9+
- pip

### Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

## Usage

### Manual Mode (Default - Most Secure)

Browser opens for manual login - supports 2FA and security keys:

```bash
# Backup current filters
python -m src.main backup

# Generate consolidated Sieve
python -m src.main consolidate --backup latest --output filters.sieve

# Preview changes
python -m src.main diff --backup latest

# Apply changes (disable old filters, upload Sieve)
python -m src.main sync --sieve filters.sieve --backup latest

# If needed, restore to previous state
python -m src.main restore --backup backups/<timestamp>.json
```

### Automated Mode (For Testing/CI)

Uses credentials from `.credentials` file for headless operation:

```bash
# Backup with automated login
python -m src.main backup --headless --credentials-file .credentials

# All commands support --headless and --credentials-file flags
python -m src.main consolidate --backup latest --output filters.sieve
python -m src.main sync --sieve filters.sieve --backup latest --headless --credentials-file .credentials
```

## Commands

### `backup`

Scrape and save current filters to a timestamped backup.

```bash
python -m src.main backup [OPTIONS]

Options:
  --headless              Run browser in headless mode
  --credentials-file TEXT Path to credentials file (default: .credentials)
  --no-credentials        Force manual login
```

### `list-backups`

Show all available backups with statistics.

```bash
python -m src.main list-backups
```

### `analyze`

View filter statistics and consolidation opportunities.

```bash
python -m src.main analyze --backup latest [OPTIONS]
```

### `consolidate`

Generate optimized Sieve script from backup (local only, no ProtonMail changes).

```bash
python -m src.main consolidate --backup latest --output filters.sieve [OPTIONS]

Options:
  --backup TEXT       Backup identifier (timestamp or "latest")
  --output TEXT       Output file for Sieve script
  --strategies TEXT   Consolidation strategies (comma-separated)
```

### `diff`

Compare backups or current state vs backup to preview changes.

```bash
python -m src.main diff [OPTIONS]

Options:
  --backup TEXT           Compare current vs backup
  --backup1 TEXT          Compare two backups
  --backup2 TEXT          Compare two backups
  --headless              Run browser in headless mode
  --credentials-file TEXT Credentials file for current state comparison
```

### `sync`

Upload Sieve script and disable old UI filters (reversible).

```bash
python -m src.main sync --sieve filters.sieve --backup latest [OPTIONS]

Options:
  --sieve TEXT            Path to Sieve script to upload
  --backup TEXT           Backup to reference for disabling filters
  --headless              Run browser in headless mode
  --credentials-file TEXT Credentials file
```

### `restore`

Restore filters to previous backup state (re-enable/disable as needed).

```bash
python -m src.main restore --backup <timestamp> [OPTIONS]

Options:
  --backup TEXT           Backup timestamp to restore from
  --headless              Run browser in headless mode
  --credentials-file TEXT Credentials file
```

### `cleanup`

Delete all disabled filters (optional, with confirmation).

```bash
python -m src.main cleanup [OPTIONS]

Options:
  --dry-run               Preview what will be deleted without deleting
  --headless              Run browser in headless mode
  --credentials-file TEXT Credentials file
```

## Credentials File Format

Create `.credentials` file for automated testing:

```
Username: your_username
Password: your_password
```

⚠️ **NEVER commit `.credentials` file to git!** It's in `.gitignore`.

## Backup File Format

Backups are stored in `backups/` with this structure:

```json
{
  "version": "1.0",
  "timestamp": "2026-02-08T19:30:45.123456",
  "metadata": {
    "filter_count": 253,
    "enabled_count": 253,
    "disabled_count": 0,
    "account_email": "[email protected]",
    "tool_version": "0.1.0"
  },
  "filters": [
    {
      "name": "Work emails",
      "enabled": true,
      "priority": 1,
      "logic": "and",
      "conditions": [...],
      "actions": [...]
    }
  ],
  "checksum": "sha256:..."
}
```

## Consolidation Example

**Before** (3 separate filters):
```
Filter 1: from = "alice@company.com" → move to "Work"
Filter 2: from = "bob@company.com" → move to "Work"
Filter 3: from = "charlie@company.com" → move to "Work"
```

**After** (1 consolidated Sieve rule):
```sieve
require "fileinto";

# Work emails (consolidated from 3 filters)
if address :is "from" ["alice@company.com", "bob@company.com", "charlie@company.com"] {
    fileinto "INBOX.Work";
}
```

## How It Works

### Safe Workflow

1. **Create backup**: Scrapes current filters, saves to timestamped JSON
2. **Analyze**: Review filter patterns and consolidation opportunities
3. **Consolidate**: Generate optimized Sieve script (local only)
4. **Diff**: Compare backup vs current state to see what will change
5. **Sync**: Upload Sieve and disable old UI filters (old filters preserved, just disabled)
6. **Verify**: Check that consolidation worked as expected
7. **Restore** (if needed): Re-enable/disable filters to match backup if something goes wrong

### Consolidation Strategies

The tool applies multiple strategies to minimize the resulting Sieve:

- **Group by Action**: Merge filters with same action using OR logic
- **Merge Conditions**: Convert multiple conditions to arrays
- **Optimize Ordering**: Place important rules first, use `stop` statements

## Testing

### Unit Tests

```bash
pytest tests/ -v
```

### End-to-End Integration Test

```bash
bash test_workflow.sh
```

This runs a complete cycle: backup → consolidate → sync → restore → verify

## Architecture

- **src/models/**: Data models (Pydantic)
- **src/scraper/**: ProtonMail browser automation (Playwright)
- **src/parser/**: Convert scraped data to models
- **src/backup/**: Backup management, diff engine, restore logic
- **src/consolidator/**: Filter optimization strategies
- **src/generator/**: Sieve script generation
- **src/main.py**: CLI entry point

## Troubleshooting

### "Login failed" error

- **Manual mode**: Check that you're entering credentials correctly
- **Automated mode**: Verify `.credentials` file exists and has correct format
- **2FA enabled**: Disable 2FA for test account or use manual mode

### Playwright browser not found

```bash
playwright install chromium
```

### ProtonMail UI changed

If selectors don't work:
1. Inspect ProtonMail filter page: Settings → Filters
2. Update selectors in `src/scraper/selectors.py`
3. Test with a small backup first

## Contributing

To add new consolidation strategies or features, see `ARCHITECTURE.md`.

## License

MIT

## Safety Reminders

- 🔒 Keep `.credentials` file secure and never commit it
- 💾 Always backup before running sync
- 👁️ Review diff output before applying changes
- 📝 Check generated Sieve script before upload
- ↩️ Know how to restore if something goes wrong

---

**Questions?** Check the inline documentation in source files or review test files for usage examples.
