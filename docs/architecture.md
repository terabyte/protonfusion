# Architecture

This document describes the architecture of ProtonFusion, a CLI tool for consolidating ProtonMail filters into optimized Sieve scripts.

## High-Level Overview

ProtonFusion follows a pipeline architecture: scrape filters from ProtonMail's web UI, parse them into structured models, consolidate redundant filters, generate Sieve scripts, and optionally sync them back.

```
ProtonMail UI ──► Scraper ──► Parser ──► Models ──► Consolidator ──► Sieve Generator
                                                         │                    │
                                                    Backup Manager       Sync Engine
                                                    (snapshots/)         (upload + disable)
```

All operations are organized around **snapshots** -- timestamped directories that capture the full state of your filters at a point in time.

## Module Structure

```
src/
├── main.py                    # CLI entry point (Typer)
├── models/                    # Pydantic v2 data models
│   ├── filter_models.py       # ProtonMailFilter, ConsolidatedFilter, enums
│   └── backup_models.py       # Backup, BackupMetadata
├── scraper/                   # Playwright browser automation
│   ├── browser.py             # ProtonMailBrowser base class (login, navigation)
│   ├── selectors.py           # Centralized CSS selectors for ProtonMail UI
│   ├── protonmail_scraper.py  # Read-only filter scraping (parallel-capable)
│   └── protonmail_sync.py     # Write operations (create/delete/enable/disable, Sieve upload)
├── parser/
│   └── filter_parser.py       # Convert scraped HTML data into Pydantic models
├── backup/
│   ├── backup_manager.py      # Snapshot creation, loading, listing, manifests
│   ├── diff_engine.py         # Compare two filter states
│   └── restore_engine.py      # Re-enable/disable filters to match a backup state
├── consolidator/
│   ├── consolidation_engine.py  # Main pipeline: strategy composition + reporting
│   └── strategies/
│       ├── group_by_action.py     # Merge filters with identical actions
│       ├── merge_conditions.py    # Combine compatible single-condition groups
│       └── optimize_ordering.py   # Sort rules by action priority
├── generator/
│   └── sieve_generator.py    # Generate RFC 5228 Sieve scripts
└── utils/
    └── config.py              # Paths, URLs, timeouts, credential loading
```

## Data Models

All data models use **Pydantic v2** for validation and serialization.

### Filter Models (`src/models/filter_models.py`)

The core domain model is `ProtonMailFilter`, representing a single filter as it exists in ProtonMail's UI:

```
ProtonMailFilter
├── name: str
├── enabled: bool
├── priority: int
├── logic: LogicType (AND | OR)
├── conditions: List[FilterCondition]
│   └── FilterCondition { type: ConditionType, operator: Operator, value: str }
└── actions: List[FilterAction]
    └── FilterAction { type: ActionType, parameters: dict }
```

After consolidation, filters are represented as `ConsolidatedFilter`:

```
ConsolidatedFilter
├── name: str
├── condition_groups: List[ConditionGroup]   # OR'd together
│   └── ConditionGroup { logic: LogicType, conditions: List[FilterCondition] }
├── actions: List[FilterAction]
├── source_filters: List[str]                # names of original filters
└── filter_count: int
```

The key insight is the **ConditionGroup** abstraction. When multiple filters are merged, each original filter's conditions become a separate group. Groups are OR'd together (any match triggers the action), while conditions within a group retain their original AND/OR logic.

### Enums

| Enum | Values |
|------|--------|
| `ConditionType` | sender, recipient, subject, attachments, header |
| `Operator` | contains, is, matches, starts_with, ends_with, has |
| `ActionType` | move_to, label, mark_read, star, archive, delete |
| `LogicType` | and, or |

### Content Hashing

`ProtonMailFilter.content_hash` produces a SHA-256 hash of the filter's identity (name, logic, conditions, actions) but **excludes** enabled state and priority. This allows the manifest system to track which filters have been processed regardless of whether they've been disabled since.

## Scraper Layer

### Browser Automation

The scraper uses **Playwright** to automate Chromium. ProtonMail has no public filter management API, so browser automation is the only option.

**`ProtonMailBrowser`** is the base class shared by the scraper and sync engine. It handles:
- Login (automated via credentials file, or manual with a 2-minute timeout)
- Navigation to the filters settings page (inbox → gear icon → "All settings" → "Filters" sidebar link)
- Reading/writing Sieve scripts via the CodeMirror 5 JavaScript API

**`ProtonMailScraper`** (read-only) scrapes filter details by opening each filter's edit modal and stepping through the wizard (Name → Conditions → Actions). It supports parallel scraping across multiple browser tabs.

**`ProtonMailSync`** (write operations) handles creating, deleting, enabling, and disabling filters, as well as uploading Sieve scripts.

### Centralized Selectors

All CSS selectors for ProtonMail's UI are defined in **`selectors.py`**. This makes it straightforward to update when ProtonMail changes their UI -- you only need to update one file.

### Parallel Scraping

Scraping is slow (~5 seconds per filter due to modal transitions). To speed this up, the scraper can open N browser tabs within the same `BrowserContext` (shared session cookies). Each tab independently navigates to the filters page and scrapes its assigned chunk. Results are merged by index to preserve priority ordering.

Only read-only operations are parallelized. Write operations remain sequential to avoid race conditions on shared server state. See [optimization.md](optimization.md) for performance analysis.

## Consolidation Engine

The consolidation engine applies a pipeline of three composable strategies:

### Strategy 1: Group by Action (`group_by_action.py`)

Filters with identical actions are merged into a single `ConsolidatedFilter`. Each original filter's conditions become a `ConditionGroup`.

Example: 50 filters that all do "move to Spam" → 1 consolidated filter with 50 condition groups.

### Strategy 2: Merge Conditions (`merge_conditions.py`)

Within a consolidated filter, single-condition groups with the same type and operator are merged into pipe-delimited arrays.

**Safe merge:**
```
Group 1: sender contains "alice"    →  sender contains "alice|bob"
Group 2: sender contains "bob"
```

**Not merged** (multi-condition groups are never flattened):
```
Group 1: sender contains "alice" AND subject contains "urgent"
Group 2: sender contains "bob"
```

This preserves exact behavioral equivalence.

### Strategy 3: Optimize Ordering (`optimize_ordering.py`)

Rules are sorted by action priority (delete > archive > move > label > mark_read > star), with a secondary sort by filter count. This ensures the most impactful rules (like spam deletion) are evaluated first.

### Adding New Strategies

Each strategy is a function with the signature `List[ConsolidatedFilter] → List[ConsolidatedFilter]`. New strategies can be added to `consolidation_engine.py` without modifying existing ones.

## Sieve Generator

The generator converts `ConsolidatedFilter` objects into RFC 5228 Sieve scripts. Key behaviors:

- **Extension collection**: Scans all filters for required Sieve extensions (fileinto, imap4flags, regex) and generates the appropriate `require` statement.
- **Pipe-delimited arrays**: Values like `"alice|bob"` expand to Sieve arrays `["alice", "bob"]`.
- **Section markers**: Generated rules are wrapped in `# === BEGIN ProtonFusion ===` / `# === END ProtonFusion ===` markers.
- **Merging**: When uploading to an account that already has a Sieve script, content outside the markers is preserved. Require statements are deduplicated.

### Sieve Mapping

| Filter Concept | Sieve Output |
|----------------|-------------|
| sender is "X" | `address :is "From" "X"` |
| recipient contains "X" | `address :contains "To" "X"` |
| subject matches "X" | `header :matches "Subject" "X"` |
| move to Folder | `fileinto "Folder";` |
| label "X" | `fileinto "X";` |
| mark as read | `addflag "\\Seen";` |
| star | `addflag "\\Flagged";` |
| archive | `fileinto "Archive";` |
| delete | `discard;` |

## Snapshot System

All data is stored under `snapshots/` (overridable via `PROTONFUSION_DATA_DIR` environment variable). Each run creates a timestamped directory:

```
snapshots/
├── 2026-02-11_08-16-55/
│   ├── backup.json           # Filter data + Sieve script + SHA-256 checksum
│   ├── consolidated.sieve    # Generated Sieve rules
│   └── manifest.json         # Sync tracking metadata
├── 2026-02-12_14-30-00/
│   └── ...
└── latest -> 2026-02-12_14-30-00/   # Symlink to newest snapshot
```

### backup.json

Contains the full Pydantic-serialized `Backup` object: metadata (filter counts, account email, tool version), the list of `ProtonMailFilter` objects, the existing Sieve script (captured from the account at backup time), and a SHA-256 checksum for integrity verification.

### manifest.json

Tracks the consolidation and sync lifecycle:

```json
{
  "created_at": "2026-02-12T14:30:00Z",
  "filter_hashes": ["a1b2c3d4...", "e5f6g7h8..."],
  "filter_names": ["Filter A", "Filter B"],
  "filter_count": 2,
  "sieve_file": "consolidated.sieve",
  "synced_at": null
}
```

`synced_at` starts as `null` and is set to an ISO timestamp when the Sieve script is successfully uploaded. The `filter_hashes` allow the consolidation engine to recognize previously processed filters -- if you re-run consolidation after a sync, disabled filters whose hashes appear in a prior synced manifest can optionally be re-included.

### Diff Engine

The diff engine compares two filter states (backup vs. backup, or backup vs. current) and categorizes differences as: added, removed, modified, state_changed (only enabled/disabled toggled), or unchanged.

### Restore Engine

The restore engine takes a backup and the current filter state, then enables or disables filters to match the backup. It reports on filters that were not found (deleted since backup), already correct, successfully toggled, or errored.

## CLI Layer

The CLI is built with **Typer** and uses **Rich** for terminal output (tables, panels, colored text). All commands that interact with ProtonMail accept `--headless`, `--credentials-file`, `--manual-login`, and `--workers` flags.

Commands are organized by their relationship to the data flow:
- **Read**: `show`, `show-backup`, `list-snapshots`, `analyze`, `diff`
- **Write local**: `backup`, `consolidate`
- **Write remote**: `sync`, `restore`, `cleanup`
