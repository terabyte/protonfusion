# Design Decisions

This document explains the rationale behind key design choices in ProtonFusion.

## Why Browser Automation?

ProtonMail does not provide a public API for managing filters. The only way to read and manipulate filters programmatically is through the web UI. Playwright was chosen over Selenium for its async-first design, built-in auto-wait capabilities, and reliable Chromium support.

The downside is fragility -- ProtonMail can change their UI at any time and break selectors. To mitigate this, all CSS selectors are centralized in a single file (`src/scraper/selectors.py`), and the scraper includes structural assertions (`_assert_filter_page_structure()`) that fail loudly when the expected page layout changes.

## Non-Destructive by Default

Every design choice prioritizes reversibility:

- **Disable, don't delete.** When syncing, old UI filters are disabled rather than deleted. This means you can always re-enable them manually or via the `restore` command, which also puts back the ProtonFusion script captured in that backup (see [Restore Is a Full Rollback](#restore-is-a-full-rollback)). See [Which Filters Sync Disables](#which-filters-sync-disables).
- **Snapshot-based operations.** Every action references a snapshot. You never modify filter data in place -- you create a new snapshot directory.
- **Section markers in Sieve.** Generated Sieve rules are wrapped in `# === BEGIN/END ProtonFusion ===` markers. User-authored Sieve rules outside these markers are preserved during merge. This allows ProtonFusion to coexist with hand-written Sieve rules.
- **Refuse rather than drop.** `sync` compares the live ProtonFusion section with the new one and refuses, before disabling or uploading anything, if any rule would disappear. See [Refusing to Drop Live Rules](#refusing-to-drop-live-rules).
- **Never delete the last copy.** `cleanup` only deletes a disabled UI filter whose rules are all present in the live ProtonFusion section.
- **Dry-run mode.** The `sync` and `cleanup` commands support `--dry-run` to preview changes before committing.
- **Checksums.** Every backup includes a SHA-256 checksum, and `load_backup` verifies it, so every command refuses a `backup.json` that changed after it was written. A hand edit fails this too, on purpose: it can change what a filter does. The global `--ignore-checksum` (before the command name) loads one anyway, with unknown values flagged incomplete rather than guessed.
- **Incomplete reads are loud.** The scraper once read only the folder and mark-as rows of the Actions step, so every "Label as" action was silently dropped from backups and Sieve, and `cleanup` then deleted the only copy. Now anything the scraper cannot parse marks the filter incomplete, `backup` refuses to save it without `--allow-incomplete`, `consolidate` leaves it out of the script (what was read of a filter can be wider than the filter: an AND filter missing a condition matches more, and one whose only condition was dropped matches everything) unless given `--allow-incomplete`, each filter keeps the wizard's raw text as evidence, and `cleanup` refuses to delete a filter without a complete, evidence-bearing backup copy.

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

## Four-State Filter Lifecycle

### Why Not Just Enabled/Disabled?

After consolidating and syncing, the typical workflow involves running `cleanup` to delete disabled UI filters from ProtonMail (freeing the limited filter slots). But the next `backup` no longer sees those deleted filters, so `consolidate` loses their rules. A two-state model (enabled/disabled) doesn't capture the distinction between "live on ProtonMail" and "preserved locally".

The four-state model solves this:
- **enabled/disabled** — live UI filters, scraped during backup
- **archived** — baked into Sieve only; no longer on ProtonMail but carried forward locally
- **deprecated** — excluded from everything; kept for reference only

### Immutable backup.json

`backup.json` is a faithful scrape record and is never modified after creation. When a user changes a filter's status (e.g., `snapshot set-status "Filter X" deprecated`), the change is stored as an `ArchiveEntry` in `archive.json`. This means you can always inspect the raw backup to see exactly what was on ProtonMail at that point in time.

### Archive Carry-Forward

On every `backup`, `archive.json` is copied from the previous snapshot (via the `latest` symlink) into the new snapshot directory. This ensures archived filters persist indefinitely across backup cycles without user intervention. The carry-forward happens before the `latest` symlink is updated to avoid self-copy.

### Post-Consolidation Auto-Archiving

When `consolidate` runs, backup filters that were included in Sieve generation are automatically moved to `archive.json` as `archived`. This prepares the archive for the next cycle: after `sync` and `cleanup` remove UI filters, the next backup won't find them, but the archive still has them. Inclusion is tracked by `content_hash`, never by name, so a disabled filter that shares a name with an included one is not archived (and its rule does not reach the next script).

`cleanup` archives too, but only after the deletion is confirmed and only the filters it is deleting: a dry run or a declined prompt writes nothing, and a filter it refuses is not archived (its live copy would otherwise become the "verified backup copy" the next run checks for). A filter deleted with `--include-uncovered` is archived as `deprecated`, not `archived`: its rules are not in the live section and it was disabled, so consolidating it would switch on a rule the user had switched off.

### Refusing to Drop Live Rules

The archive only protects rules that went through it. Rules consolidated before the archive system existed, or whose `archive.json` was lost, live only in the ProtonFusion section of the live Sieve script once `cleanup` has deleted their UI filters. The next backup -> consolidate -> sync would regenerate the section from the few surviving UI filters and delete them. For example, a section built from a couple of hundred filters, rebuilt from the handful of UI filters created since the last cleanup.

So `sync` treats the live section as data, not as output to overwrite. It parses both sections into condition/action pairs and refuses if any live pair is missing from the new one (details and limits in [sieve-reference.md](sieve-reference.md#rule-preservation)). The comparison is structural rather than a text diff because consolidation legitimately regroups, reorders and re-merges rules on every run; a text diff would cry wolf on every sync and get overridden by reflex. Anything the parser does not model is compared verbatim, so unfamiliar constructs cause a refusal rather than a silent pass. The check runs before any filter is disabled, so a refusal leaves the account untouched, and `cleanup` independently checks each disabled filter against the live section before deleting it, so a refused or failed sync can never be followed by deleting the only copy.

`--allow-rule-removal` overrides the refusal. Removing a rule therefore takes an explicit act: deprecate it (`snapshot set-status ... deprecated`) or exclude it, then sync with the override.

### Carrying Forward Live Rules (`consolidate --keep-live-rules`)

Refusing is only half the fix; there must also be a supported way to keep the rules. The options considered:

1. **Splice the live rules into the new section as text.** Simple, but the spliced rules would never re-enter the model: they would not be consolidated with new filters, not be visible in `snapshot view`, not be deprecatable, and would have to be re-spliced from the live script on every run forever.
2. **Have `sync` merge (union) the live and new sections at upload time.** This hides the problem at the last step, makes the uploaded script differ from the reviewed `consolidated.sieve`, and makes it impossible to ever remove a rule.
3. **Rebuild the missing rules as filters and store them in the archive.** Chosen.

With `--keep-live-rules`, `consolidate` compares the new section with the live section captured in the backup, converts each dropped condition/action pair back into a `ProtonMailFilter` (the inverse of the generator), and adds them to `archive.json` with status `archived` and a name starting `Carried forward (<snapshot>):`. Consolidation then runs again with them included. The result is a union of the scraped filters and the live section, but the union lives in the model, so:

- the carried rules consolidate with everything else and show up in `snapshot view`;
- every later backup inherits them through the normal archive carry-forward, so the flag is needed once to repair an account, not on every run;
- they can be removed the normal way (`snapshot set-status <name> deprecated`, or `snapshot remove`).

Rules the user removed on purpose are not resurrected: pairs belonging to deprecated filters or to filters named by `--exclude` are skipped. Conversion is verified, not trusted: each rebuilt filter is regenerated and must yield exactly the pairs it was built from. Anything that cannot round-trip (`stop`, `redirect`, unmodelled tests, values containing `|` or `, `) is listed as unconvertible and left out, so `sync` still refuses until the user moves those rules outside the markers by hand.

It is opt-in rather than the default because it changes `archive.json`, and because the refusal already makes the default path loud: `consolidate` warns and `sync` refuses, both naming the flag. Test values come back lowercased, which matches Sieve's default case-insensitive comparison.

### Backward Compatibility

The `enabled: bool` field is preserved on `ProtonMailFilter` for backward compatibility with existing serialized data. A `@model_validator(mode='before')` derives `status` from `enabled` when loading old data that lacks a `status` field, and keeps `enabled` in sync when `status` is set explicitly. The `content_hash` excludes both `enabled` and `status` so manifest tracking is unaffected by status transitions.

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

## Which Filters Sync Disables

ProtonMail limits active filters per plan, so `sync` disables UI filters before uploading the Sieve filter. It used to disable every enabled row, which turned off other Sieve filters (and ProtonFusion's own, leaving nothing filtering mail if the upload then failed) and silently stopped any filter created after the backup, whose rule was never consolidated.

Now `sync` scrapes the live filters and disables only wizard filters whose `content_hash` (name, logic, conditions, actions) is in the set the script was built from: the snapshot manifest's `filter_hashes`, or every wizard filter in the `--backup` snapshot when `--sieve` names a script with no manifest. Matching by content rather than name means a filter edited since the backup stays on. Sieve filters, unmatched filters, and rows the scraper could not read in full are left enabled and listed, so a mismatch errs toward a rule running twice, never toward a rule not running. `--dry-run` shows the plan from the backup and `--show-diff-only` from the live account.

Rows are toggled by scraped position, confirmed by name (`set_row_enabled`), since names need not be unique, and each switch is read back after the click, so a click that did not take counts as a failure rather than a done toggle. If the upload fails, every row this run disabled is re-enabled; any that cannot be are listed with the `restore` command. A missing "Add sieve filter" button is reported as the probable active-filter limit, with the filters left enabled as the ones to disable or fold in. `sync` never falls back to disabling everything.

## Free Tier Limitations

ProtonMail's free tier allows only 1 custom filter at a time. Both the "Add filter" and "Add sieve filter" buttons disappear once a filter exists. The sync workflow accounts for this by disabling the UI filters the script replaces before creating the Sieve filter, freeing the slot.

## CodeMirror 5 Integration

ProtonMail's Sieve editor uses CodeMirror 5. Typing into the editor via Playwright's keyboard API doesn't trigger CodeMirror's change detection, leaving the Save button disabled. Instead, the scraper uses the JavaScript API directly:

```javascript
document.querySelector('.CodeMirror').CodeMirror.setValue(script)
```

This properly triggers change events and enables the Save button.

## Restore Is a Full Rollback

`restore --backup <snapshot>` puts back both halves of a sync: the UI filters' on/off states and the `ProtonFusion Consolidated` script (`backup.json`'s `sieve_script`). It previews, asks, and saves a safety backup of the current state first (not made `latest`, since after the restore it no longer describes the account), so the restore can itself be undone.

The order is chosen so a failure part-way never leaves mail unfiltered: enable the filters the backup has on, then replace the script, then disable the filters the backup has off. Until the last step every rule from both the current and the restored state is active, so the worst a failure leaves is a rule applied twice. It stops at the first failed enable or a failed upload (an upload that raises is reported as leaving the script in an unknown state) and reports what was done.

That ordering only covers a failure part-way. A completed restore switches off whatever the backed-up state does not have, and after `cleanup` (which deletes UI filters once the live section holds their rules) or an edit since the backup, the backed-up script can lack rules whose filter restore cannot switch back on. So before changing anything restore compares the live ProtonFusion section with the script that will filter mail afterwards (the backed-up one, or none if the backup has ProtonFusion's filter off) plus every wizard filter that will be on, and refuses, listing each rule found in neither, unless `--allow-rule-removal`. The guarantee is therefore: no rule ends up in neither place without that flag, and no failure part-way switches off a rule that was active.

Filters are matched by content hash and toggled by row position confirmed by name, as `sync` does; a Sieve filter is matched by name, since its script is exactly what may differ. If the backup holds no script while the account has one, restore refuses unless `--allow-empty-script`, which disables the ProtonFusion filter rather than uploading an empty script.

