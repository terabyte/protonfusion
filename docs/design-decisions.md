# Design Decisions

This document explains the rationale behind key design choices in ProtonFusion.

## Why Browser Automation?

ProtonMail does not provide a public API for managing filters. The only way to read and manipulate filters programmatically is through the web UI. Playwright was chosen over Selenium for its async-first design, built-in auto-wait capabilities, and reliable Chromium support.

The downside is fragility -- ProtonMail can change their UI at any time and break selectors. To mitigate this, all CSS selectors are centralized in a single file (`src/scraper/selectors.py`), and the scraper includes structural assertions (`_assert_filter_page_structure()`) that fail loudly when the expected page layout changes.

## Non-Destructive by Default

Every design choice prioritizes reversibility:

- **Disable, don't delete.** When syncing, old UI filters are disabled rather than deleted. This means you can always re-enable them manually or via the `restore` command.
- **Snapshot-based operations.** Every action references a snapshot. You never modify filter data in place -- you create a new snapshot directory.
- **Section markers in Sieve.** Generated Sieve rules are wrapped in `# === BEGIN/END ProtonFusion ===` markers. User-authored Sieve rules outside these markers are preserved during merge. This allows ProtonFusion to coexist with hand-written Sieve rules.
- **Dry-run mode.** The `sync` and `cleanup` commands support `--dry-run` to preview changes before committing.
- **Checksums.** Every backup includes a SHA-256 checksum so corruption can be detected.

## Snapshot Architecture (vs. Single Backup File)

Early prototypes used a single `backups/` directory with individual JSON files. This was replaced with the snapshot directory approach because:

1. **Atomic grouping.** A backup, its consolidated Sieve script, and its manifest naturally belong together. A directory groups them without inventing naming conventions.
2. **Sync tracking.** The manifest tracks whether a Sieve script has been uploaded. This needs to live alongside the specific backup and Sieve file it refers to.
3. **Clean listing.** `list-snapshots` just iterates directories rather than parsing filenames.
4. **Latest symlink.** A `latest` symlink provides a stable reference without maintaining a separate state file.

## Consolidation Strategy Pipeline

The three-strategy pipeline was designed to be:

- **Composable.** Each strategy transforms `List[ConsolidatedFilter] → List[ConsolidatedFilter]`. Strategies can be reordered, removed, or added without changing the engine.
- **Behavior-preserving.** The consolidation must never change what messages are matched. This is why multi-condition groups are never flattened -- an AND group must stay AND, even when merged with other filters.
- **Conservative.** The merge_conditions strategy only merges single-condition groups with identical type and operator. This is the only safe merge; anything more complex risks changing behavior.

### Why ConditionGroups?

When filter A has "sender=alice AND subject=urgent" and filter B has "sender=bob", merging them must not create "sender=alice|bob AND subject=urgent" (which would incorrectly require both conditions for bob). Instead, each filter becomes a ConditionGroup that preserves its internal logic, and groups are OR'd together.

## Dependency Choices

### Playwright (browser automation)

Chosen for its first-class async support, auto-wait mechanisms, and reliable Chromium control. Playwright's `BrowserContext` feature is essential for parallel scraping -- multiple tabs share login state without separate authentication.

### Pydantic v2 (data models)

Provides validation, serialization, and type safety for filter data flowing through the pipeline. v2's performance improvements matter when processing hundreds of filters. `model_dump()` and `model_validate()` make JSON round-tripping clean.

### Typer (CLI framework)

Built on Click, Typer provides type-annotated CLI parameter definitions with automatic help text and error messages. The `>=0.15.0` requirement ensures compatibility with Click 8.x.

**Typer quirks:** `Optional[str]` parameters cause "secondary flag" errors in some versions; the workaround is to use `str` with `""` default. Boolean parameters named `--no-X` conflict with Typer's auto-generated `--no-` variants.

### Rich (terminal UI)

Provides tables, panels, colored output, and progress spinners. Used throughout the CLI for displaying filter lists, diff results, analysis reports, and sync progress.

### python-dotenv (configuration)

Lightweight environment variable loading. Used primarily for `PROTONFUSION_DATA_DIR` test isolation.

## Parallel Scraping Design

Scraping filters sequentially takes ~5 seconds per filter (3 wizard steps with modal transitions). With 250 filters, that's ~21 minutes.

The parallel solution opens N tabs within the same BrowserContext:
- Tabs share the login session (no re-authentication)
- Each tab independently navigates to the filters page
- Filters are divided by index across workers
- Results are merged by index to preserve priority ordering

Only read-only operations are parallelized. Write operations (disable, delete, upload) remain sequential because:
- Disabling filters causes DOM reflows that invalidate other tabs' element references
- Deleting filters shifts row indices
- Sieve upload is a single operation with no parallelism benefit

## Folder Path Resolution

ProtonMail's dropdown UI displays subfolder names with a bullet prefix (`• Child Folder`), but Sieve `fileinto` requires the full path (`Parent/Child`). The scraper builds a path map by reading dropdown items in display order -- non-bulleted items are tracked as the current parent, and bulleted items are mapped to `Parent/Child` paths. This map is cached per scraper instance and built lazily on the first folder action encounter.

## Free Tier Limitations

ProtonMail's free tier allows only 1 custom filter at a time. Both the "Add filter" and "Add sieve filter" buttons disappear once a filter exists. The sync workflow accounts for this by disabling existing UI filters before creating the Sieve filter, freeing the slot.

## CodeMirror 5 Integration

ProtonMail's Sieve editor uses CodeMirror 5. Typing into the editor via Playwright's keyboard API doesn't trigger CodeMirror's change detection, leaving the Save button disabled. Instead, the scraper uses the JavaScript API directly:

```javascript
document.querySelector('.CodeMirror').CodeMirror.setValue(script)
```

This properly triggers change events and enables the Save button.
