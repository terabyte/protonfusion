"""CLI entry point for ProtonFusion."""

import asyncio
import difflib
import functools
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markup import escape
from rich import print as rprint

from src.utils.config import (
    load_credentials, SNAPSHOTS_DIR, TOOL_VERSION,
)
from src.models.filter_models import ProtonMailFilter, FilterStatus
from src.models.backup_models import Backup, ArchiveEntry
from src.backup.backup_manager import BackupManager, BackupIntegrityError, unverified_for_deletion
from src.backup.diff_engine import DiffEngine
from src.backup.sync_plan import DisablePlan, carried_hashes, plan_disable
from src.utils.private_files import write_private_file
from src.parser.filter_parser import parse_scraped_filters
from src.consolidator.consolidation_engine import ConsolidationEngine
from src.generator.sieve_generator import SieveGenerator, SieveGenerationError, SECTION_BEGIN
from src.generator.sieve_rules import SieveParseError, compare_sections, extract_section, script_facts
from src.consolidator.carry_forward import facts_to_filters, filter_facts, is_carried, label_targets

SIEVE_FILTER_NAME = "ProtonFusion Consolidated"
STATE_HELP = (
    "Saved session file from 'login' (default: $PROTONFUSION_STORAGE_STATE, "
    "else ~/.config/protonfusion/storage_state.json); used when present"
)


app = typer.Typer(
    name="protonfusion",
    help="ProtonFusion - safely consolidate your ProtonMail filters into Sieve scripts.",
    add_completion=False,
)
snapshot_app = typer.Typer(help="Manage snapshot contents.")
app.add_typer(snapshot_app, name="snapshot")
console = Console()


@app.callback()
def _global_options(
    ignore_checksum: bool = typer.Option(
        False, "--ignore-checksum",
        help="Load backups whose checksum does not match (e.g. hand-edited) instead of refusing. "
             "Give it before the command name.",
    ),
):
    """ProtonFusion - safely consolidate your ProtonMail filters into Sieve scripts."""
    # Set (not just enabled) on every run, so one invocation's override
    # never carries into the next in the same process.
    BackupManager.ignore_checksum = ignore_checksum

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _print_incomplete(filters: List[ProtonMailFilter], heading: str):
    """List filters the scraper could not fully read, with each reason."""
    console.print(f"[bold red]{heading}")
    for f in filters:
        console.print(f"  [red]- {escape(f.name)}")
        for issue in f.scrape_issues:
            console.print(f"      {escape(issue)}")


def _without_evidence(filters: List[ProtonMailFilter]) -> List[ProtonMailFilter]:
    """Filters with no raw scrape evidence, i.e. from a backup made before format 1.1.

    They report no scrape issues only because the old scraper recorded none;
    that is the scraper which silently dropped labels, so they are treated as
    incomplete. Carried-forward filters were never scraped and are exempt.
    """
    return [f for f in filters if f.raw is None and not is_carried(f)]


def _get_credentials(credentials_file: str, manual_login: bool):
    """Load credentials if applicable."""
    if manual_login:
        return None
    if credentials_file:
        return load_credentials(credentials_file)
    return None


DEFAULT_LOGIN_TIMEOUT_S = 600


def _run_browser_command(coro):
    """asyncio.run a browser command, turning expected failures into a clean exit 1.

    A dead or missing session, and a live Sieve script that could not be read.
    Every command reads the live script before it changes or saves anything,
    so a failed read stops the run with the account and snapshots untouched,
    rather than being taken for "no script".
    """
    from src.scraper.browser import SessionExpiredError, SieveReadError

    try:
        return asyncio.run(coro)
    except SessionExpiredError as e:
        console.print(f"[red]{e}")
        raise typer.Exit(1)
    except SieveReadError as e:
        console.print(
            f"[bold red]Could not read the live Sieve script ({escape(str(e))}).[/] "
            "Refusing to continue: treating it as empty could lose the rules in it. "
            "Nothing was changed or saved; try again."
        )
        raise typer.Exit(1)


@app.command()
def login(
    credentials_file: str = typer.Option("", "--credentials-file", help="Pre-fill the login form from this credentials file"),
    state: str = typer.Option("", "--state", help="Where to save the session (default: $PROTONFUSION_STORAGE_STATE, else ~/.config/protonfusion/storage_state.json)"),
    timeout: int = typer.Option(DEFAULT_LOGIN_TIMEOUT_S, "--timeout", help="Seconds to wait for you to finish signing in"),
):
    """Sign in once in a visible browser and save the session for other commands.

    Proton shows a Human Verification CAPTCHA to automated logins, so sign in
    here by hand (CAPTCHA, 2FA); the saved session then lets backup, show,
    sync, etc. run without logging in, headless included, until Proton
    expires it. The session file holds live auth cookies and is written 0600.
    """
    from src.scraper.browser import ProtonMailBrowser

    creds = _get_credentials(credentials_file, False)

    async def _run():
        browser = ProtonMailBrowser(headless=False, credentials=creds, storage_state_path=state or None)
        try:
            await browser.initialize(load_storage_state=False)
            await browser.interactive_login(timeout_ms=timeout * 1000)
            path = await browser.save_storage_state()
            return path, browser.account_slot, browser.account_email
        finally:
            await browser.close()

    try:
        path, slot, email = asyncio.run(_run())
    except RuntimeError as e:
        console.print(f"[red]{e}")
        raise typer.Exit(1)

    lines = [f"[bold green]Session saved to {path}[/]"]
    if email:
        lines.append(f"Account: {email}")
    lines.append(f"Session slot: /u/{slot}/")
    lines.append("\nOther commands will reuse it; run 'login' again when it expires.")
    console.print(Panel("\n".join(lines), title="Logged In"))


@app.command()
def backup(
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Path to credentials file"),
    state: str = typer.Option("", "--state", help=STATE_HELP),
    manual_login: bool = typer.Option(False, "--manual-login", help="Force manual login"),
    output: str = typer.Option("", "--output", help="Custom output path for backup file"),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete",
        help="Save the snapshot even if some filters could not be fully read (they are flagged in backup.json)",
    ),
):
    """Scrape current filters and save to a timestamped snapshot.

    Fails (exit 1, nothing saved) if any filter could not be fully read,
    unless --allow-incomplete is given, and always if the live Sieve script
    could not be read.
    """
    from src.scraper.protonmail_scraper import ProtonMailScraper

    creds = _get_credentials(credentials_file, manual_login)
    workers = max(1, min(workers, 10))

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            with console.status("[bold green]Initializing browser..."):
                await scraper.initialize()

            with console.status("[bold green]Logging in..."):
                await scraper.login()

            with console.status("[bold green]Navigating to filters..."):
                await scraper.navigate_to_filters()

            if workers > 1:
                console.print(f"[bold green]Scraping filters with {workers} parallel tabs...")
            else:
                console.print("[bold green]Scraping filters...")
            raw_filters = await scraper.scrape_all_filters(workers=workers)
            console.print(f"[green]Scraped {len(raw_filters)} filters")

            # Parse filters
            filters = parse_scraped_filters(raw_filters)

            # A filter the scraper could not fully read must not be saved
            # as though it were whole: consolidate would build Sieve without
            # the missing parts, and cleanup would then delete the only
            # complete copy. Refuse unless the user explicitly accepts it.
            # A filter that could not be parsed at all is here too, as a
            # flagged stub (parse_scraped_filters never drops one).
            incomplete = [f for f in filters if not f.is_complete]
            if incomplete:
                _print_incomplete(
                    incomplete,
                    f"{len(incomplete)} filter(s) could not be fully read:",
                )
                if not allow_incomplete:
                    console.print(
                        "[bold red]Backup NOT saved.[/] Their actions or conditions may be incomplete, "
                        "so a Sieve script built from them could silently drop behaviour.\n"
                        "Re-run with --allow-incomplete to save anyway; the filters are flagged in "
                        "backup.json and cleanup will refuse to delete them."
                    )
                    raise typer.Exit(1)
                console.print("[yellow]--allow-incomplete given: saving with these filters flagged.")

            # Raises SieveReadError on a failed read (not "" as for no
            # script), which _run_browser_command turns into exit 1 before
            # anything is saved.
            with console.status("[bold green]Reading existing Sieve script..."):
                sieve_script = await scraper.read_sieve_script(
                    filter_name=SIEVE_FILTER_NAME,
                )

            # Create backup
            manager = BackupManager()
            bkup = manager.create_backup(
                filters,
                account_email=scraper.account_email,
                sieve_script=sieve_script,
            )

            backup_lines = [
                f"[bold green]Backup created successfully![/]\n",
                f"Filters: {bkup.metadata.filter_count}",
                f"Enabled: {bkup.metadata.enabled_count}",
                f"Disabled: {bkup.metadata.disabled_count}",
                f"Checksum: {bkup.checksum[:30]}...",
            ]
            if incomplete:
                backup_lines.append(f"[yellow]Incomplete (flagged): {len(incomplete)}[/]")
            if sieve_script:
                backup_lines.append(f"\nSieve script captured: {len(sieve_script)} chars")
                if SECTION_BEGIN not in sieve_script:
                    backup_lines.append(
                        "[yellow]Warning: existing script has no ProtonFusion markers.[/]\n"
                        "[yellow]Running 'sync' will wrap it outside the managed section.[/]"
                    )
                preview = "\n".join(sieve_script.split("\n")[:5])
                backup_lines.append(f"\n[dim]Preview:[/]\n[dim]{preview}[/]")

            console.print(Panel("\n".join(backup_lines), title="Backup Complete"))
        finally:
            await scraper.close()

    _run_browser_command(_run())


@app.command()
def show(
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Path to credentials file"),
    state: str = typer.Option("", "--state", help=STATE_HELP),
    manual_login: bool = typer.Option(False, "--manual-login", help="Force manual login"),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
):
    """Read and display your current filters (read-only, no changes made).

    This is a safe way to verify the tool can connect to your account and
    read your filters before running any other commands.
    """
    from src.scraper.protonmail_scraper import ProtonMailScraper

    creds = _get_credentials(credentials_file, manual_login)
    workers = max(1, min(workers, 10))

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            with console.status("[bold green]Initializing browser..."):
                await scraper.initialize()

            with console.status("[bold green]Logging in..."):
                await scraper.login()

            with console.status("[bold green]Navigating to filters..."):
                await scraper.navigate_to_filters()

            if workers > 1:
                console.print(f"[bold green]Scraping filters with {workers} parallel tabs...")
            raw_filters = await scraper.scrape_all_filters(workers=workers)

            filters = parse_scraped_filters(raw_filters)
            _display_filters(filters)

        finally:
            await scraper.close()

    _run_browser_command(_run())


@app.command("show-backup")
def show_backup(
    backup_id: str = typer.Option("latest", "--backup", help="Backup identifier (timestamp or 'latest')"),
):
    """Display filters from a backup file (offline, no login needed)."""
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)
    _display_filters(bkup.filters, source=f"backup '{backup_id}'")
    if bkup.sieve_script:
        console.print(f"\n[cyan]Backup includes Sieve script ({len(bkup.sieve_script)} chars)")
        has_markers = SECTION_BEGIN in bkup.sieve_script
        console.print(f"[cyan]ProtonFusion markers: {'yes' if has_markers else 'no'}")


_STATUS_DISPLAY = {
    FilterStatus.ENABLED: "[green]enabled[/]",
    FilterStatus.DISABLED: "[yellow]disabled[/]",
    FilterStatus.ARCHIVED: "[cyan]archived[/]",
    FilterStatus.DEPRECATED: "[dim]deprecated[/]",
}


def _display_filters(filters: list, source: str = "ProtonMail account"):
    """Display a list of filters in a readable table."""
    if not filters:
        console.print(f"[yellow]No filters found in {source}.")
        return

    counts = {}
    for f in filters:
        counts[f.status.value] = counts.get(f.status.value, 0) + 1

    summary_parts = [f"[bold]Found {len(filters)} filters[/] in {source}"]
    for status_val, color in [("enabled", "green"), ("disabled", "yellow"), ("archived", "cyan"), ("deprecated", "dim")]:
        count = counts.get(status_val, 0)
        if count > 0:
            summary_parts.append(f"{status_val.title()}: [{color}]{count}[/]")

    console.print(Panel(
        "\n".join(summary_parts[:1]) + "\n" + "  ".join(summary_parts[1:]),
        title="Filter Summary",
    ))

    table = Table(title="Filters")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name", style="cyan", max_width=40)
    table.add_column("Status", justify="center")
    table.add_column("Conditions", max_width=50)
    table.add_column("Actions", max_width=30)

    for i, f in enumerate(filters, 1):
        status = _STATUS_DISPLAY.get(f.status, str(f.status.value))

        # Format conditions
        cond_parts = []
        for c in f.conditions:
            cond_parts.append(f"{c.type.value} {c.operator.value} \"{c.value}\"")
        conds_str = f" {f.logic.value.upper()} ".join(cond_parts) if cond_parts else "[dim]none[/]"

        # Format actions
        action_parts = []
        for a in f.actions:
            if a.parameters:
                params = ", ".join(f"{v}" for v in a.parameters.values())
                action_parts.append(f"{a.type.value}({params})")
            else:
                action_parts.append(a.type.value)
        actions_str = ", ".join(action_parts) if action_parts else "[dim]none[/]"

        name_str = escape(f.name) if f.is_complete else f"{escape(f.name)} [red](incomplete)[/]"
        table.add_row(str(i), name_str, status, conds_str, actions_str)

    console.print(table)


@app.command("list-snapshots")
def list_snapshots():
    """Show all available snapshots with statistics."""
    manager = BackupManager()
    backups = manager.list_backups()

    if not backups:
        console.print("[yellow]No snapshots found. Run 'backup' first.")
        return

    table = Table(title="Available Snapshots")
    table.add_column("Snapshot", style="cyan")
    table.add_column("Timestamp", style="green")
    table.add_column("Filters", justify="right")
    table.add_column("Enabled", justify="right", style="green")
    table.add_column("Disabled", justify="right", style="yellow")
    table.add_column("Archived", justify="right", style="cyan")
    table.add_column("Size", justify="right")

    for b in backups:
        size_kb = b["size_bytes"] / 1024
        # Load archive count for this snapshot
        snapshot_path = Path(b["path"])
        archive_entries = manager.load_archive(snapshot_path)
        archived_count = len(archive_entries)
        table.add_row(
            b["snapshot"],
            b["timestamp"][:19] if b["timestamp"] else "?",
            str(b["filter_count"]),
            str(b["enabled_count"]),
            str(b["disabled_count"]),
            str(archived_count) if archived_count else "-",
            f"{size_kb:.1f} KB",
        )

    console.print(table)


# Keep list-backups as alias
@app.command("list-backups", hidden=True)
def list_backups():
    """Show all available snapshots (alias for list-snapshots)."""
    list_snapshots()


@app.command()
def analyze(
    backup_id: str = typer.Option("latest", "--backup", help="Backup identifier (timestamp or 'latest')"),
    include_disabled: bool = typer.Option(False, "--include-disabled", help="Include disabled filters in analysis"),
):
    """Analyze filter patterns and consolidation opportunities."""
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)

    synced_filter_hashes = None
    if not include_disabled:
        synced_filter_hashes = manager.load_synced_hashes()
        if synced_filter_hashes:
            console.print(f"[cyan]Including previously synced filters from manifest ({len(synced_filter_hashes)} hashes)")

    engine = ConsolidationEngine()
    stats = engine.analyze(
        bkup.filters,
        include_disabled=include_disabled,
        synced_filter_hashes=synced_filter_hashes,
    )

    console.print(Panel(
        f"[bold]Filter Statistics[/]\n\n"
        f"Total filters: {stats['total_filters']}\n"
        f"Enabled: {stats['enabled']}\n"
        f"Disabled: {stats['disabled']}",
        title="Analysis",
    ))

    if stats["action_distribution"]:
        table = Table(title="Action Distribution")
        table.add_column("Action", style="cyan")
        table.add_column("Count", justify="right")
        for action, count in stats["action_distribution"].items():
            table.add_row(action, str(count))
        console.print(table)

    if stats["condition_distribution"]:
        table = Table(title="Condition Type Distribution")
        table.add_column("Type", style="cyan")
        table.add_column("Count", justify="right")
        for ctype, count in stats["condition_distribution"].items():
            table.add_row(ctype, str(count))
        console.print(table)

    if stats["consolidation_opportunities"]:
        table = Table(title="Consolidation Opportunities")
        table.add_column("Same Action", style="cyan")
        table.add_column("Filters", justify="right", style="green")
        for action, count in stats["consolidation_opportunities"].items():
            table.add_row(action, str(count))
        console.print(table)
        console.print(f"\n[bold green]Potential reduction: ~{stats['potential_reduction']} fewer filters")
    else:
        console.print("[yellow]No consolidation opportunities found.")


@app.command()
def consolidate(
    backup_id: str = typer.Option("latest", "--backup", help="Backup identifier"),
    output_file: str = typer.Option("", "--output", help="Output file for Sieve script (default: inside snapshot dir)"),
    include_disabled: bool = typer.Option(False, "--include-disabled", help="Include disabled filters in consolidation"),
    exclude: Optional[List[str]] = typer.Option(None, "--exclude", help="Exclude filter by name (repeatable)"),
    include_args_from: str = typer.Option("", "--include-args-from", help="Load previous consolidation_args.json from snapshot"),
    keep_live_rules: bool = typer.Option(
        False, "--keep-live-rules",
        help="Carry forward rules from the live ProtonFusion Sieve section (as captured in the backup) "
             "that this consolidation would otherwise drop, saving them to archive.json",
    ),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete",
        help="Also build rules from filters that were not fully read when backed up "
             "(what was read may be wider than the real filter)",
    ),
):
    """Generate optimized Sieve script from backup (local only, no ProtonMail changes).

    Filters that were not fully read when backed up are left out of the
    script and listed (and recorded in manifest.json), since what was read
    of one can match more mail than the real filter. --allow-incomplete
    includes them anyway, with a warning.

    After 'cleanup', the live ProtonFusion section may be the only copy of some
    rules. Without --keep-live-rules, consolidation warns when it would drop any
    of them (and 'sync' refuses). With it, those rules are rebuilt as archived
    filters so they stay in the section from now on.
    """
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)
    snapshot_dir = manager.snapshot_dir_for(backup_id)

    # Build exclude set from CLI args + loaded args
    exclude_names: set[str] = set(exclude) if exclude else set()
    if include_args_from:
        args_dir = manager.snapshot_dir_for(include_args_from)
        args_path = args_dir / "consolidation_args.json"
        if args_path.exists():
            saved_args = json.loads(args_path.read_text())
            exclude_names.update(saved_args.get("exclude", []))
            console.print(f"[cyan]Loaded args from {include_args_from}: +{len(saved_args.get('exclude', []))} excludes")
        else:
            console.print(f"[yellow]No consolidation_args.json found in {include_args_from}")

    # Load archive entries and separate by status
    archive_entries = manager.load_archive(snapshot_dir)
    archived_filters = [
        e.filter for e in archive_entries
        if e.filter.status == FilterStatus.ARCHIVED
    ]

    # Apply archive status overrides to backup filters
    archive_by_hash = {e.filter.content_hash: e for e in archive_entries}
    backup_filters = []
    for f in bkup.filters:
        if f.content_hash in archive_by_hash:
            # Archive entry overrides this backup filter's status
            override = archive_by_hash[f.content_hash]
            if override.filter.status == FilterStatus.ARCHIVED:
                continue  # Already in archived_filters
            if override.filter.status == FilterStatus.DEPRECATED:
                continue  # Will be skipped
            backup_filters.append(override.filter)
        else:
            backup_filters.append(f)

    if archived_filters:
        console.print(f"[cyan]Including {len(archived_filters)} archived filters from archive")
    if exclude_names:
        console.print(f"[cyan]Excluding by name: {', '.join(sorted(exclude_names))}")

    synced_filter_hashes = None
    if not include_disabled:
        synced_filter_hashes = manager.load_synced_hashes()
        if synced_filter_hashes:
            console.print(f"[cyan]Including previously synced filters from manifest ({len(synced_filter_hashes)} hashes)")

    engine = ConsolidationEngine()
    generator = SieveGenerator()

    def _consolidate():
        consolidated, report = engine.consolidate(
            backup_filters,
            include_disabled=include_disabled,
            synced_filter_hashes=synced_filter_hashes,
            archived_filters=archived_filters,
            exclude_names=exclude_names,
            allow_incomplete=allow_incomplete,
        )
        return consolidated, report, generator.generate(consolidated)

    try:
        consolidated, report, sieve_script = _consolidate()
    except SieveGenerationError as e:
        console.print(f"[red]{escape(str(e))}")
        raise typer.Exit(1)

    # Compare against the live ProtonFusion section captured at backup time.
    # After `cleanup` it may be the only copy of some rules.
    carried_count = 0
    live_script = bkup.sieve_script or ""
    if SECTION_BEGIN in live_script:
        try:
            comparison = compare_sections(live_script, sieve_script)
        except SieveParseError as e:
            comparison = None
            console.print(f"[red]Could not parse the ProtonFusion section in the backup: {escape(str(e))}")
            console.print("[yellow]'sync' will refuse until this is resolved.")

        if comparison is not None and not comparison.is_safe and keep_live_rules:
            # Rules the user removed on purpose (deprecated, or --exclude'd)
            # must not be resurrected from the live section.
            intentionally_removed = [
                e.filter for e in archive_entries if e.filter.status == FilterStatus.DEPRECATED
            ] + [
                f for f in list(bkup.filters) + archived_filters if f.name in exclude_names
            ]
            suppressed = set()
            for f in intentionally_removed:
                suppressed |= filter_facts(f)
            to_carry = [fact for fact in comparison.dropped if fact not in suppressed]

            # The backup and archive say which fileinto targets are labels
            known_labels = label_targets(list(bkup.filters) + [e.filter for e in archive_entries])
            carried, unconvertible = facts_to_filters(
                to_carry, label=snapshot_dir.name, label_names=known_labels,
            )
            known_hashes = {e.filter.content_hash for e in archive_entries}
            now_ts = datetime.now(timezone.utc).isoformat()
            for f in carried:
                if f.content_hash in known_hashes:
                    continue
                archive_entries.append(ArchiveEntry(
                    filter=f, archived_at=now_ts, source_snapshot=snapshot_dir.name,
                ))
                archived_filters.append(f)
                carried_count += 1

            if carried_count:
                consolidated, report, sieve_script = _consolidate()
            console.print(
                f"[cyan]Carried forward {len(to_carry) - len(unconvertible)} condition/action pairs "
                f"from the live Sieve section as {carried_count} archived filters"
            )
            if len(to_carry) != len(comparison.dropped):
                console.print(
                    f"[cyan]Not carried forward (deprecated or --exclude'd on purpose): "
                    f"{len(comparison.dropped) - len(to_carry)} pairs"
                )
            if unconvertible:
                console.print(
                    f"[red]{len(unconvertible)} live condition/action pairs could not be converted back into filters "
                    "and are NOT in the new section ('sync' will refuse):"
                )
                for fact in unconvertible:
                    console.print(f"  [red]- {escape(fact.describe())}")
                console.print(
                    "[yellow]Move them outside the ProtonFusion markers by hand to keep them."
                )
        elif comparison is not None and not comparison.is_safe:
            console.print(Panel(
                f"[bold red]This consolidation drops {len(comparison.dropped)} condition/action pairs "
                "that are in the live ProtonFusion section captured in the backup.[/]\n"
                "If their UI filters were deleted by 'cleanup', the live section is their only copy.\n\n"
                "'sync' will refuse to upload this. To keep them, re-run with --keep-live-rules.\n"
                "Run 'sync --dry-run' for the full list.",
                title="Live Rules Would Be Dropped", border_style="red",
            ))

    if report.incomplete_excluded:
        _print_incomplete(
            report.incomplete_excluded,
            f"Left out of the script: {len(report.incomplete_excluded)} filter(s) not fully read "
            "when backed up. Their rules are NOT in the generated Sieve:",
        )
        console.print(
            "[yellow]What was read of a filter can match more mail than the filter itself (a "
            "dropped condition widens it), so it is not used. 'sync' leaves these filters enabled. "
            "Fix the cause and run 'backup' again, or pass --allow-incomplete to include them as read."
        )
    if report.incomplete_included:
        _print_incomplete(
            report.incomplete_included,
            f"WARNING: --allow-incomplete given: {len(report.incomplete_included)} filter(s) not fully "
            "read are IN the script as read. Their rules may be missing parts, or match more mail "
            "than the real filter (a delete rule would delete more). Check them before 'sync':",
        )

    if output_file:
        out_path = Path(output_file)
    else:
        out_path = snapshot_dir / "consolidated.sieve"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_private_file(out_path, sieve_script)
    console.print(f"[green]Sieve script saved to: {out_path}")

    # The filters that went into the script, by content_hash. Not by name:
    # a disabled filter sharing a name with an included one would otherwise
    # be recorded as synced and archived as active, and its rule would come
    # back live in the next script.
    processed_filters = []
    in_script_hashes = set()
    for f in report.selected:
        if f.content_hash not in in_script_hashes:
            in_script_hashes.add(f.content_hash)
            processed_filters.append(f)
    without_evidence = _without_evidence(processed_filters)
    if without_evidence:
        console.print(
            f"[bold red]Warning: {len(without_evidence)} filter(s) in this script come from a backup "
            "made before format 1.1 and have no raw evidence. Labels or other actions the old "
            "scraper missed are not in them:"
        )
        for f in without_evidence:
            console.print(f"  [red]- {escape(f.name)}")
        console.print(
            "[yellow]'sync' will refuse this script unless given --allow-incomplete. "
            "Run 'backup' again, then 'consolidate', to fix it."
        )
    manager.write_manifest(
        snapshot_dir, processed_filters, str(out_path),
        without_evidence=[f.name for f in without_evidence],
        incomplete_excluded=report.incomplete_excluded,
        incomplete_included=report.incomplete_included,
    )
    console.print(f"[cyan]Manifest written to snapshot ({len(processed_filters)} filters)")

    # Post-consolidation archiving: move included backup filters to archive
    # (once per hash: identical duplicates in the backup are one rule)
    now_ts = datetime.now(timezone.utc).isoformat()
    archived_hashes = {e.filter.content_hash for e in archive_entries}
    for f in bkup.filters:
        if f.content_hash in in_script_hashes and f.content_hash not in archived_hashes:
            archived_hashes.add(f.content_hash)
            archived_f = f.model_copy(deep=True)
            archived_f.status = FilterStatus.ARCHIVED
            archived_f.enabled = False
            archive_entries.append(ArchiveEntry(
                filter=archived_f,
                archived_at=now_ts,
                source_snapshot=snapshot_dir.name,
            ))
    manager.write_archive(snapshot_dir, archive_entries)

    # Save consolidation_args.json
    args_data = {
        "exclude": sorted(exclude_names) if exclude_names else [],
        "include_disabled": include_disabled,
        "keep_live_rules": keep_live_rules,
        "allow_incomplete": allow_incomplete,
        "carried_forward": carried_count,
        "created_at": now_ts,
    }
    write_private_file(snapshot_dir / "consolidation_args.json", json.dumps(args_data, indent=2))

    # Build report display
    report_lines = [
        f"[bold]Consolidation Report[/]\n",
        f"Original filters: {report.original_count}",
        f"Processed: {report.enabled_count}",
        f"Disabled (skipped): {report.disabled_skipped}",
    ]
    if report.disabled_included > 0:
        report_lines.append(f"Disabled (included via manifest): {report.disabled_included}")
    if report.archived_count > 0:
        report_lines.append(f"Archived (included): {report.archived_count}")
    if report.excluded_count > 0:
        report_lines.append(f"Excluded by name: {report.excluded_count}")
    if report.sieve_skipped > 0:
        report_lines.append(f"Sieve filters (left as they are): {report.sieve_skipped}")
    if report.incomplete_excluded:
        report_lines.append(f"[red]Not fully read (left out): {len(report.incomplete_excluded)}[/]")
    if report.incomplete_included:
        report_lines.append(f"[red]Not fully read (INCLUDED, --allow-incomplete): {len(report.incomplete_included)}[/]")
    if carried_count > 0:
        report_lines.append(f"Carried forward from live Sieve (new archived filters): {carried_count}")
    report_lines.append(f"Consolidated rules: {report.consolidated_count}")
    report_lines.append(f"[bold green]Reduction: {report.reduction_percent:.1f}%[/]")

    console.print(Panel("\n".join(report_lines), title="Consolidation Complete"))

    if len(sieve_script) < 3000:
        console.print(Panel(sieve_script, title="Generated Sieve Script", border_style="blue"))
    else:
        preview = "\n".join(sieve_script.split("\n")[:30])
        console.print(Panel(preview + "\n...", title="Sieve Script Preview (first 30 lines)", border_style="blue"))


@app.command()
def diff(
    backup_id: str = typer.Option("", "--backup", help="Compare current state vs this backup"),
    backup1: str = typer.Option("", "--backup1", help="First backup for comparison"),
    backup2: str = typer.Option("", "--backup2", help="Second backup for comparison"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    state: str = typer.Option("", "--state", help=STATE_HELP),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
):
    """Compare backups or current state vs backup."""
    manager = BackupManager()
    diff_engine = DiffEngine()

    if backup1 and backup2:
        b1 = manager.load_backup(backup1)
        b2 = manager.load_backup(backup2)
        result = diff_engine.compare_backups(b1, b2)
        _display_diff(result, diff_engine, f"Diff: {backup1} vs {backup2}")

    elif backup_id:
        from src.scraper.protonmail_scraper import ProtonMailScraper

        creds = _get_credentials(credentials_file, False)
        _workers = max(1, min(workers, 10))
        bkup = manager.load_backup(backup_id)

        async def _run():
            scraper = ProtonMailScraper(headless=headless, credentials=creds, storage_state_path=state or None)
            try:
                await scraper.initialize()
                await scraper.login()
                await scraper.navigate_to_filters()
                raw_filters = await scraper.scrape_all_filters(workers=_workers)
                current_filters = parse_scraped_filters(raw_filters)
                result = diff_engine.compare_filter_lists(bkup.filters, current_filters)
                _display_diff(result, diff_engine, f"Diff: {backup_id} vs Current")
            finally:
                await scraper.close()

        _run_browser_command(_run())
    else:
        console.print("[red]Provide --backup (compare vs current) or --backup1/--backup2 (compare two backups)")
        raise typer.Exit(1)


def _display_diff(diff_result, diff_engine: DiffEngine, title: str):
    """Display diff results with colors."""
    summary = diff_engine.generate_summary(diff_result)

    if summary["total_changes"] == 0:
        console.print(Panel("[bold green]No differences found!", title=title))
        return

    table = Table(title=title)
    table.add_column("Change", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("[green]Added[/green]", str(summary["added"]))
    table.add_row("[red]Removed[/red]", str(summary["removed"]))
    table.add_row("[yellow]Modified[/yellow]", str(summary["modified"]))
    table.add_row("[blue]State Changed[/blue]", str(summary["state_changed"]))
    table.add_row("Unchanged", str(summary["unchanged"]))
    console.print(table)

    if diff_result.added:
        console.print("\n[bold green]Added filters:")
        for f in diff_result.added[:10]:
            console.print(f"  [green]+ {f.name}")
        if len(diff_result.added) > 10:
            console.print(f"  ... and {len(diff_result.added) - 10} more")

    if diff_result.removed:
        console.print("\n[bold red]Removed filters:")
        for f in diff_result.removed[:10]:
            console.print(f"  [red]- {f.name}")
        if len(diff_result.removed) > 10:
            console.print(f"  ... and {len(diff_result.removed) - 10} more")

    if diff_result.modified:
        console.print("\n[bold yellow]Modified filters:")
        for old, new in diff_result.modified[:10]:
            console.print(f"  [yellow]~ {old.name}")

    if diff_result.state_changed:
        console.print("\n[bold blue]State changed:")
        for old, new in diff_result.state_changed[:10]:
            state = "enabled" if new.enabled else "disabled"
            console.print(f"  [blue]  {old.name} -> {state}")


def _print_carry_forward_note(snapshot_dir: Path) -> None:
    """Tell the user how many filters the last consolidate carried forward from live Sieve."""
    args_path = snapshot_dir / "consolidation_args.json"
    if not args_path.exists():
        return
    args = json.loads(args_path.read_text())
    if args.get("keep_live_rules"):
        console.print(
            f"[cyan]consolidate --keep-live-rules was used: {args.get('carried_forward', 0)} "
            "archived filters were carried forward from the live Sieve section into this script."
        )


def _rule_preservation_check(
    live_script: str,
    new_script: str,
    allow_rule_removal: bool,
    backup_script: str = "",
    live_label: str = "live",
) -> bool:
    """Compare the live ProtonFusion section with the new one and print the result.

    Returns True if it is safe to replace the live section, False if sync must
    refuse. After `cleanup`, the live section is the only copy of rules whose UI
    filters were deleted, so replacing it with a section rebuilt from fewer
    filters would silently delete them. Also refuses when the live script reads
    back empty although the backup shows it had a ProtonFusion section, since an
    empty read is indistinguishable from a failed one.

    `allow_rule_removal` turns a refusal into a warning.
    """
    if not live_script and backup_script and SECTION_BEGIN in backup_script:
        console.print(Panel(
            f"[bold red]The {live_label} Sieve script read back empty, but the backup "
            "shows it contained a ProtonFusion section.[/]\n"
            "Either the read failed or the script was removed. Replacing it now could "
            "delete rules that exist nowhere else.",
            title="Rule Preservation Check", border_style="red",
        ))
        if allow_rule_removal:
            console.print("[yellow]--allow-rule-removal given: proceeding anyway.")
            return True
        return False

    try:
        result = compare_sections(live_script, new_script)
    except SieveParseError as e:
        console.print(Panel(
            f"[bold red]Could not parse the ProtonFusion section: {escape(str(e))}[/]\n"
            "Without a structural comparison there is no way to tell whether the new "
            "section drops rules.",
            title="Rule Preservation Check", border_style="red",
        ))
        if allow_rule_removal:
            console.print("[yellow]--allow-rule-removal given: proceeding anyway.")
            return True
        return False

    summary = (
        f"{live_label.capitalize()} ProtonFusion section: {result.live_rule_count} rules "
        f"({result.live_fact_count} condition/action pairs)\n"
        f"New ProtonFusion section: {result.new_rule_count} rules "
        f"({result.new_fact_count} condition/action pairs)\n"
        f"Added: {len(result.added)}   Dropped: {len(result.dropped)}"
    )
    if result.wildcard_fixes:
        fix_lines = [
            "",
            f"[yellow]Corrected: {len(result.wildcard_fixes)} begins-with/ends-with conditions.[/]",
            "Older ProtonFusion versions wrote these without the * wildcard, so they "
            "only matched the exact value. The new section adds the wildcard; these "
            "are not dropped rules.",
        ]
        for old, new in result.wildcard_fixes:
            fix_lines.append(f"  [yellow]~ {escape(old.describe())}[/]")
            fix_lines.append(f"    [green]{escape(new.describe())}[/]")
        summary += "\n" + "\n".join(fix_lines)
    if result.is_safe:
        console.print(Panel(
            f"[bold green]No rules dropped.[/]\n\n{summary}",
            title="Rule Preservation Check", border_style="green",
        ))
        return True

    lines = [f"[bold red]The new section would drop {len(result.dropped)} "
             f"condition/action pairs present in the {live_label} section.[/]\n", summary, ""]
    for actions, conditions in result.dropped_by_action().items():
        lines.append(f"[bold]{escape(actions)}[/]  ({len(conditions)} dropped)")
        for cond in conditions:
            lines.append(f"  [red]- {escape(cond)}[/]")
    if result.opaque_live_rules:
        lines.append("")
        lines.append("[yellow]Note: some live rules use constructs ProtonFusion does not "
                     "generate; they only count as kept if copied verbatim.[/]")
    console.print(Panel("\n".join(lines), title="Rule Preservation Check", border_style="red"))

    if allow_rule_removal:
        console.print("[yellow]--allow-rule-removal given: these rules will be removed.")
        return True
    console.print(
        "[yellow]To keep them, re-run 'consolidate --keep-live-rules' to carry the live "
        "rules forward.\n"
        "If removing them is intended, re-run sync with --allow-rule-removal."
    )
    return False


def _print_disable_plan(plan: DisablePlan, backup_id: str, preview: bool) -> None:
    """Say which enabled UI filters sync disables, which it leaves on, and why.

    `preview` words it as what a sync would do (--dry-run, --show-diff-only).
    """
    verb = "Would disable" if preview else "Disabling"
    console.print(f"\n[bold]{verb} {len(plan.to_disable)} UI filters whose rules are in this script:")
    for f in plan.to_disable:
        console.print(f"  - {escape(f.name)}")

    leave = "would be left" if preview else "were left"
    groups = [
        (plan.after_backup, "yellow",
         f"created or changed after backup '{backup_id}' {leave} enabled; their rules are not in "
         "this script. Run 'backup' and 'consolidate' to fold them in."),
        (plan.not_in_script, "cyan",
         f"left out of this script (--exclude or deprecated) {leave} enabled."),
        (plan.unreadable, "yellow",
         f"could not be read in full, so they cannot be matched to the backup; they {leave} enabled."),
        (plan.sieve, "cyan", f"Sieve filters {leave} enabled (sync never disables these)."),
    ]
    for filters, color, text in groups:
        if not filters:
            continue
        console.print(f"[{color}]{len(filters)} {text}")
        for f in filters:
            console.print(f"  [{color}]- {escape(f.name)}")


async def _reenable_after_failed_upload(sync_client, disabled: List[ProtonMailFilter], backup_id: str) -> None:
    """Turn back on every filter this sync disabled, after its upload failed.

    Otherwise a failed upload leaves neither the old UI filters nor the new
    script handling mail. Anything that cannot be re-enabled is listed with
    the `restore` command that brings it back.
    """
    if not disabled:
        console.print("[yellow]No filters had been disabled, so nothing else changed.")
        return
    try:
        # The failed upload may have left the Sieve editor open over the list
        await sync_client.navigate_to_filters()
    except Exception as e:
        logger.warning("Could not reload the filters page before re-enabling: %s", e)

    failed = []
    for f in disabled:
        try:
            enabled = await sync_client.set_row_enabled(f.priority, f.name, True)
        except Exception as e:
            logger.warning("Re-enabling '%s' failed: %s", f.name, e)
            enabled = False
        if not enabled:
            failed.append(f)

    console.print(f"[green]Re-enabled {len(disabled) - len(failed)} of the {len(disabled)} filters this sync disabled.")
    if failed:
        console.print(f"[bold red]Could not re-enable {len(failed)} filter(s); they are still disabled:")
        for f in failed:
            console.print(f"  [red]- {escape(f.name)}")
        console.print(f"[yellow]To re-enable them, run: restore --backup {backup_id}")


def _print_carried_source(from_manifest: bool, backup_id: str) -> None:
    """Note when the filters to disable are inferred from the backup, not the manifest."""
    if not from_manifest:
        console.print(
            f"[yellow]No consolidate manifest describes this script, so every wizard filter in "
            f"backup '{backup_id}' is taken to be in it."
        )


@app.command()
def sync(
    sieve_file: str = typer.Option("", "--sieve", help="Path to Sieve script to upload (default: from snapshot)"),
    backup_id: str = typer.Option("latest", "--backup", help="Backup to reference for disabling filters"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    state: str = typer.Option("", "--state", help=STATE_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without applying"),
    show_diff_only: bool = typer.Option(False, "--show-diff-only", help="Log in, fetch live Sieve, show diff, change nothing"),
    allow_rule_removal: bool = typer.Option(
        False, "--allow-rule-removal",
        help="Proceed even if the new ProtonFusion section drops rules present in the live one",
    ),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete",
        help="Upload even if the script was built from filters with no raw evidence (pre-1.1 backups)",
    ),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
):
    """Upload Sieve script and disable the UI filters it replaces (reversible).

    Only wizard filters whose rules are in the script (same content as a
    filter 'consolidate' put into it) are disabled. Sieve filters, filters
    created or changed after the backup, and filters that cannot be read in
    full stay enabled. If the upload fails, every filter this run disabled
    is enabled again.

    Refuses (exit 1, nothing changed) if the new ProtonFusion section would drop
    any rule present in the live section, unless --allow-rule-removal is given.
    Also refuses if 'consolidate' built the script from filters with no raw
    evidence (backups made before format 1.1), unless --allow-incomplete is given.
    """
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync

    _workers = max(1, min(workers, 10))
    manager = BackupManager()
    snapshot_dir = manager.snapshot_dir_for(backup_id)

    # Resolve sieve file: explicit path or auto-discover from snapshot
    if sieve_file:
        sieve_path = Path(sieve_file)
    else:
        sieve_path = snapshot_dir / "consolidated.sieve"

    if not sieve_path.exists():
        console.print(f"[red]Sieve file not found: {sieve_path}")
        if not sieve_file:
            console.print("[yellow]Run 'consolidate' first, or provide --sieve explicitly.")
        raise typer.Exit(1)

    sieve_script = sieve_path.read_text()
    creds = _get_credentials(credentials_file, False)
    bkup = manager.load_backup(backup_id)

    # A script built from pre-1.1 filters may be missing their labels. The
    # manifest only describes the snapshot's own script, so it is checked
    # only when that is the script being uploaded.
    manifest = manager.load_manifest(snapshot_dir) or {}
    manifest_script = manifest.get("sieve_file")
    without_evidence = manifest.get("without_evidence", [])
    if without_evidence and manifest_script and Path(manifest_script).resolve() == sieve_path.resolve():
        console.print(
            f"[bold red]This script was built from {len(without_evidence)} filter(s) with no raw "
            "evidence (backed up before format 1.1); labels or other actions may be missing:"
        )
        for name in without_evidence:
            console.print(f"  [red]- {escape(name)}")
        if allow_incomplete:
            console.print("[yellow]--allow-incomplete given: proceeding anyway.")
        else:
            console.print(
                "[bold red]Sync refused. No filters were disabled and nothing was uploaded.[/]\n"
                "[yellow]Run 'backup' and 'consolidate' again, or pass --allow-incomplete."
            )
            raise typer.Exit(1)

    # Which UI filters the script replaces, by content. `reference` adds the
    # archive so a filter the script leaves out on purpose (deprecated) is
    # reported as such rather than as new since the backup.
    carried, from_manifest = carried_hashes(manifest, sieve_path, bkup.filters)
    reference = list(bkup.filters) + [e.filter for e in manager.load_archive(snapshot_dir)]

    if dry_run:
        console.print(Panel("[bold yellow]DRY RUN - No changes will be made"))
        console.print(f"\nWould upload Sieve script ({len(sieve_script)} chars)")
        _print_carried_source(from_manifest, backup_id)
        _print_disable_plan(plan_disable(bkup.filters, carried, reference, SIEVE_FILTER_NAME), backup_id, preview=True)
        console.print(
            "[cyan]This list comes from the backup. Filters created since then are not in it and "
            "would be left enabled; --show-diff-only lists them from the live account.[/]"
        )

        _print_carry_forward_note(snapshot_dir)
        console.print(
            f"\n[cyan]Comparing against the Sieve script captured in backup '{backup_id}'. "
            "Use --show-diff-only to compare against the live script.[/]"
        )
        safe = _rule_preservation_check(
            bkup.sieve_script, sieve_script, allow_rule_removal, live_label="backed-up",
        )

        # Show merge preview if backup has an existing sieve script
        if bkup.sieve_script:
            merged = SieveGenerator.merge_with_existing(sieve_script, bkup.sieve_script)
            console.print(f"\n[cyan]Existing Sieve script in backup: {len(bkup.sieve_script)} chars")
            if SECTION_BEGIN not in bkup.sieve_script:
                console.print("[yellow]User rules detected — will be preserved outside ProtonFusion section")
            if len(merged) < 3000:
                console.print(Panel(merged, title="Merged Script Preview", border_style="cyan"))
            else:
                preview = "\n".join(merged.split("\n")[:40])
                console.print(Panel(preview + "\n...", title="Merged Script Preview (first 40 lines)", border_style="cyan"))
        if not safe:
            console.print("[bold red]A real sync would REFUSE and change nothing.")
            raise typer.Exit(1)
        return

    async def _read_live():
        """Scrape the live filters and read the live script in one read-only session.

        Same order as `backup`: the wizard scrape, then the Sieve read. The
        scrape is what lets sync match rows to backed-up filters by content.
        """
        scraper = ProtonMailScraper(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            with console.status("[bold green]Reading filters to match against the backup..."):
                live_filters = parse_scraped_filters(await scraper.scrape_all_filters(workers=_workers))
            with console.status("[bold green]Reading existing Sieve script..."):
                existing_script = await scraper.read_sieve_script(filter_name=SIEVE_FILTER_NAME)
            return live_filters, existing_script or ""
        finally:
            await scraper.close()

    if show_diff_only:
        async def _show_diff():
            live_filters, existing_script = await _read_live()

            _print_carry_forward_note(snapshot_dir)
            safe = _rule_preservation_check(
                existing_script, sieve_script, allow_rule_removal,
                backup_script=bkup.sieve_script,
            )
            if not safe:
                console.print("[bold red]A real sync would REFUSE and change nothing.")
            else:
                _print_carried_source(from_manifest, backup_id)
                _print_disable_plan(
                    plan_disable(live_filters, carried, reference, SIEVE_FILTER_NAME), backup_id, preview=True,
                )

            merged_script = SieveGenerator.merge_with_existing(sieve_script, existing_script)

            if existing_script == merged_script:
                console.print(Panel("[bold green]No changes: the live script already matches."))
                return safe

            diff_lines = list(difflib.unified_diff(
                existing_script.splitlines(keepends=True),
                merged_script.splitlines(keepends=True),
                fromfile="live (ProtonMail)",
                tofile="merged (would upload)",
            ))

            if not diff_lines:
                console.print(Panel("[bold green]No changes: the live script already matches."))
                return safe

            colored = []
            for line in diff_lines:
                text = line.rstrip("\n")
                if line.startswith("+++") or line.startswith("---"):
                    colored.append(f"[bold]{text}[/bold]")
                elif line.startswith("@@"):
                    colored.append(f"[cyan]{text}[/cyan]")
                elif line.startswith("+"):
                    colored.append(f"[green]{text}[/green]")
                elif line.startswith("-"):
                    colored.append(f"[red]{text}[/red]")
                else:
                    colored.append(text)

            console.print(Panel(
                "\n".join(colored),
                title="Sieve Diff (live vs would-upload)",
                border_style="cyan",
            ))
            return safe

        if not _run_browser_command(_show_diff()):
            raise typer.Exit(1)
        return

    async def _run():
        live_filters, existing_script = await _read_live()
        if existing_script:
            console.print(f"[cyan]Found existing Sieve script ({len(existing_script)} chars)")

        # Must run before anything is disabled or uploaded: a refusal
        # leaves the account exactly as it was.
        if not _rule_preservation_check(
            existing_script, sieve_script, allow_rule_removal,
            backup_script=bkup.sieve_script,
        ):
            console.print("[bold red]Sync refused. No filters were disabled and nothing was uploaded.")
            return False

        merged_script = SieveGenerator.merge_with_existing(sieve_script, existing_script)
        if existing_script and SECTION_BEGIN not in existing_script:
            console.print("[yellow]User rules detected; preserving them outside ProtonFusion section")

        _print_carried_source(from_manifest, backup_id)
        plan = plan_disable(live_filters, carried, reference, SIEVE_FILTER_NAME)
        _print_disable_plan(plan, backup_id, preview=False)

        sync_client = ProtonMailSync(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()

            # Disable the replaced filters first: ProtonMail limits active
            # filters per plan, so a new Sieve filter can fail to save while
            # they are on. Rows are found by scraped position and name.
            disabled: List[ProtonMailFilter] = []
            not_disabled: List[ProtonMailFilter] = []
            for f in plan.to_disable:
                if await sync_client.set_row_enabled(f.priority, f.name, False):
                    disabled.append(f)
                else:
                    not_disabled.append(f)
            console.print(f"[green]Disabled {len(disabled)} filters")
            if not_disabled:
                console.print(
                    f"[yellow]Could not find {len(not_disabled)} filter(s) to disable; "
                    "they stay enabled alongside the Sieve script:"
                )
                for f in not_disabled:
                    console.print(f"  [yellow]- {escape(f.name)}")

            console.print("[bold green]Uploading merged Sieve script...")
            upload_error = None
            try:
                success = await sync_client.upload_sieve(merged_script, filter_name=SIEVE_FILTER_NAME)
            except Exception as e:
                success = False
                upload_error = e

            if not success:
                reason = f" ({escape(str(upload_error))})" if upload_error else ""
                console.print(f"[bold red]Failed to upload Sieve script{reason}.")
                if sync_client.upload_hit_filter_limit:
                    console.print(
                        "[yellow]The 'Add sieve filter' button was missing, which is how ProtonMail "
                        "shows an account at its active-filter limit. The filters left enabled above "
                        "count toward it. Disable or delete enough of them by hand (or fold them in "
                        "with 'backup' and 'consolidate'), then re-run sync."
                    )
                await _reenable_after_failed_upload(sync_client, disabled, backup_id)
                return False

            console.print("[green]Sieve script uploaded successfully!")
            if manager.promote_manifest(snapshot_dir):
                console.print("[cyan]Sync manifest updated")

            console.print(Panel(
                f"[bold green]Sync complete![/]\n\n"
                f"Sieve uploaded: Yes\n"
                f"Filters disabled: {len(disabled)}\n"
                f"Filters left enabled: {len(plan.left_enabled) + len(not_disabled)}\n\n"
                f"[yellow]To rollback, run: restore --backup {backup_id}",
                title="Sync Complete",
            ))
            return True
        finally:
            await sync_client.close()

    if not _run_browser_command(_run()):
        raise typer.Exit(1)


@app.command()
def restore(
    backup_id: str = typer.Option(..., "--backup", help="Backup to restore from"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    state: str = typer.Option("", "--state", help=STATE_HELP),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
):
    """Restore filters to previous backup state."""
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync
    from src.backup.restore_engine import RestoreEngine

    creds = _get_credentials(credentials_file, False)
    _workers = max(1, min(workers, 10))
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            raw_filters = await scraper.scrape_all_filters(workers=_workers)
            current_filters = parse_scraped_filters(raw_filters)
        finally:
            await scraper.close()

        sync_client = ProtonMailSync(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()

            restore_engine = RestoreEngine(sync_client)
            report = await restore_engine.restore_from_backup(bkup, current_filters)

            console.print(Panel(
                f"[bold green]Restore complete![/]\n\n"
                f"Enabled: {len(report['enabled'])}\n"
                f"Disabled: {len(report['disabled'])}\n"
                f"Already correct: {len(report['already_correct'])}\n"
                f"Not found: {len(report['not_found'])}\n"
                f"Errors: {len(report['errors'])}",
                title="Restore Report",
            ))

            if report["errors"]:
                console.print("\n[bold red]Errors:")
                for err in report["errors"]:
                    console.print(f"  [red]{err}")

        finally:
            await sync_client.close()

    _run_browser_command(_run())


@app.command()
def cleanup(
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    state: str = typer.Option("", "--state", help=STATE_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview what will be deleted"),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
    include_uncovered: bool = typer.Option(
        False, "--include-uncovered",
        help="Also delete disabled filters whose rules are NOT in the live ProtonFusion Sieve section",
    ),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete",
        help="Also delete disabled filters whose backup copy is missing, incomplete, or lacks raw evidence",
    ),
):
    """Delete disabled filters whose rules are already in the live Sieve script (with confirmation).

    A disabled filter is only deleted if every one of its conditions and actions
    is present in the live ProtonFusion section, so deleting it never removes
    the last copy of a rule (e.g. after a refused or failed sync). Sieve filters
    (including the ProtonFusion one) are never deleted, and neither is any filter
    whose name another filter shares, since deletion works by name. Auto-archives
    disabled filters before deletion to preserve them for future consolidation,
    except those it refuses, so a refusal holds on the next run too.
    Also refuses to delete any filter without a complete backup copy in the
    latest snapshot, unless --allow-incomplete is given.

    Exits 1 whenever it kept back a filter it would otherwise have deleted
    (uncovered, unverified, or sharing a name) or a deletion failed, so a
    script can tell a partial cleanup from a complete one. Sieve filters are
    never candidates, so leaving them alone does not count.
    """
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync

    creds = _get_credentials(credentials_file, False)
    _workers = max(1, min(workers, 10))
    manager = BackupManager()

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            raw_filters = await scraper.scrape_all_filters(workers=_workers)
            filters = parse_scraped_filters(raw_filters)
            live_script = await scraper.read_sieve_script(filter_name=SIEVE_FILTER_NAME)
        finally:
            await scraper.close()

        # A Sieve filter's rules live in its script, which the coverage and
        # backup checks below cannot see, so it is never a deletion candidate.
        # This includes SIEVE_FILTER_NAME itself, disabled by a failed sync.
        disabled_sieve = [f for f in filters if not f.enabled and f.is_sieve]
        if disabled_sieve:
            console.print("[cyan]Leaving disabled Sieve filters alone (cleanup never deletes these):")
            for f in disabled_sieve:
                console.print(f"  [cyan]- {escape(f.name)}")
        disabled = [f for f in filters if not f.enabled and not f.is_sieve]

        if not disabled:
            console.print("[green]No disabled filters to clean up.")
            return

        # Every filter kept back for safety; any at all makes the exit code 1
        held_back: List[ProtonMailFilter] = []

        # Only filters whose rules are all in the live section are safe to delete.
        # An unparsable section leaves live_facts empty, so nothing counts as covered.
        live_facts = set()
        try:
            if extract_section(live_script or "") is None:
                console.print("[yellow]No ProtonFusion section found in the live Sieve script.")
            else:
                live_facts = script_facts(live_script)
        except SieveParseError as e:
            console.print(f"[red]Could not parse the live ProtonFusion section: {escape(str(e))}")
        # A filter that was not fully read (including an unparseable stub,
        # which has no rules at all) cannot be shown covered: its parsed
        # rules are only part of it, and an empty set is trivially a subset.
        uncovered = [f for f in disabled if not f.is_complete or not filter_facts(f) <= live_facts]
        if uncovered:
            console.print(
                f"\n[bold red]{len(uncovered)} disabled filters have rules that are NOT in the "
                "live ProtonFusion Sieve section:"
            )
            for f in uncovered:
                note = "" if f.is_complete else " (not fully read, so its rules cannot be checked)"
                console.print(f"  [red]- {escape(f.name)}{note}")
            if include_uncovered:
                console.print("[yellow]--include-uncovered given: they will be deleted too.")
            else:
                console.print(
                    "[yellow]Keeping them: deleting would lose their rules. Run 'sync' first, "
                    "or pass --include-uncovered to delete them anyway."
                )
                held_back += uncovered
                disabled = [f for f in disabled if f not in uncovered]
                if not disabled:
                    console.print("[yellow]Nothing safe to delete.")
                    raise typer.Exit(1)
        # Deletion is the one irreversible step, so each filter needs a
        # backup copy known to be whole. Checked against the snapshot as it
        # was before the auto-archive below adds the live scrape to it.
        backed_up: List[ProtonMailFilter] = []
        try:
            backed_up = list(manager.load_backup("latest").filters)
            backed_up += [e.filter for e in manager.load_archive(manager.snapshot_dir_for("latest"))]
        except FileNotFoundError:
            pass  # No snapshot: every filter is unverified
        unverified = unverified_for_deletion(disabled, backed_up)
        unverified_ids = {id(f) for f, _ in unverified}

        # Auto-archive any disabled filters missing from the archive. Filters
        # this run refuses are left out: archiving the live copy would give
        # the next run the "verified backup copy" this run found missing.
        try:
            latest_dir = manager.snapshot_dir_for("latest")
            archive_entries = manager.load_archive(latest_dir)
            archive_hashes = {e.filter.content_hash for e in archive_entries}
            now_ts = datetime.now(timezone.utc).isoformat()
            auto_archived = 0
            for f in disabled:
                if id(f) in unverified_ids and not allow_incomplete:
                    continue
                if f.content_hash not in archive_hashes:
                    archived_f = f.model_copy(deep=True)
                    archived_f.status = FilterStatus.ARCHIVED
                    archived_f.enabled = False
                    archive_entries.append(ArchiveEntry(
                        filter=archived_f,
                        archived_at=now_ts,
                        source_snapshot=latest_dir.name,
                    ))
                    auto_archived += 1
            if auto_archived:
                manager.write_archive(latest_dir, archive_entries)
                console.print(f"[cyan]Auto-archived {auto_archived} filters missing from archive")
        except FileNotFoundError:
            pass  # No latest snapshot, skip archive step

        console.print(f"\n[bold yellow]Found {len(disabled)} disabled filters:")
        for f in disabled:
            console.print(f"  [yellow]- {escape(f.name)}")

        refused = []
        if unverified:
            if allow_incomplete:
                console.print(
                    f"\n[bold yellow]--allow-incomplete given: deleting {len(unverified)} filter(s) "
                    "without a verified backup copy:"
                )
            else:
                refused = unverified
                console.print(
                    f"\n[bold red]Refusing to delete {len(unverified)} filter(s) without a verified "
                    "backup copy (deleting them could lose actions the backup does not hold):"
                )
            for f, reason in unverified:
                console.print(f"  [red]- {escape(f.name)}[/]: {escape(reason)}")
            if refused:
                console.print("[yellow]Re-run 'backup', or pass --allow-incomplete to delete them anyway.")
                held_back += [f for f, _ in refused]

        to_delete = [f for f in disabled if not (refused and id(f) in unverified_ids)]

        # delete_filter() finds its row by name, so a filter whose name any
        # other scraped filter shares (enabled or disabled, covered or not,
        # Sieve or wizard) is held back: the wrong one could go.
        name_counts = Counter(f.name for f in filters)
        same_name = [f for f in to_delete if name_counts[f.name] > 1]
        if same_name:
            console.print(
                f"\n[bold red]Keeping {len(same_name)} filter(s) whose name is shared with another "
                "filter (deletion works by name, so the wrong one could go):"
            )
            for f in same_name:
                console.print(f"  [red]- {escape(f.name)}")
            console.print("[yellow]Rename them in ProtonMail so each name is unique, then re-run.")
            held_back += same_name
            to_delete = [f for f in to_delete if name_counts[f.name] == 1]

        if dry_run:
            console.print(f"\n[bold yellow]DRY RUN - No filters will be deleted ({len(to_delete)} would be).")
            if held_back:
                raise typer.Exit(1)
            return

        if not to_delete:
            console.print("[yellow]Nothing safe to delete.")
            raise typer.Exit(1)

        confirm = typer.confirm(f"\nDelete {len(to_delete)} disabled filters? This cannot be undone!")
        if not confirm:
            console.print("[yellow]Cleanup cancelled.")
            if held_back:
                raise typer.Exit(1)
            return

        sync_client = ProtonMailSync(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()

            deleted_count = 0
            for f in to_delete:
                if await sync_client.delete_filter(f.name):
                    deleted_count += 1
                    console.print(f"  [red]Deleted: {escape(f.name)}")

            console.print(f"\n[green]Deleted {deleted_count}/{len(to_delete)} filters")
        finally:
            await sync_client.close()

        not_deleted = len(held_back) + len(to_delete) - deleted_count
        if not_deleted:
            console.print(f"[bold red]{not_deleted} filter(s) were not deleted (see above).")
            raise typer.Exit(1)

    _run_browser_command(_run())


# --- Snapshot sub-commands ---

def _load_merged_filters(manager: BackupManager, backup_id: str) -> tuple[List[ProtonMailFilter], List[ArchiveEntry], Path]:
    """Load backup + archive and return merged view.

    Returns (merged_filters, archive_entries, snapshot_dir).
    Archive entries override backup entries with the same content_hash.
    """
    snapshot_dir = manager.snapshot_dir_for(backup_id)
    bkup = manager.load_backup(backup_id)
    archive_entries = manager.load_archive(snapshot_dir)

    # Index archive entries by content_hash
    archive_by_hash = {e.filter.content_hash: e for e in archive_entries}

    merged = []
    seen_hashes = set()

    # Archive entries take precedence
    for entry in archive_entries:
        merged.append(entry.filter)
        seen_hashes.add(entry.filter.content_hash)

    # Add backup filters not already in archive
    for f in bkup.filters:
        if f.content_hash not in seen_hashes:
            merged.append(f)
            seen_hashes.add(f.content_hash)

    return merged, archive_entries, snapshot_dir


@snapshot_app.command("view")
def snapshot_view(
    backup_id: str = typer.Option("latest", "--backup", help="Backup identifier (timestamp or 'latest')"),
):
    """View all filters in a snapshot (backup + archive merged)."""
    manager = BackupManager()
    merged, archive_entries, snapshot_dir = _load_merged_filters(manager, backup_id)

    if not merged:
        console.print("[yellow]No filters found in snapshot.")
        return

    # Count by status
    counts = {}
    for f in merged:
        counts[f.status.value] = counts.get(f.status.value, 0) + 1

    summary_parts = []
    for status_val in ["enabled", "disabled", "archived", "deprecated"]:
        count = counts.get(status_val, 0)
        if status_val == "enabled":
            summary_parts.append(f"Enabled: [green]{count}[/]")
        elif status_val == "disabled":
            summary_parts.append(f"Disabled: [yellow]{count}[/]")
        elif status_val == "archived":
            summary_parts.append(f"Archived: [cyan]{count}[/]")
        elif status_val == "deprecated":
            summary_parts.append(f"Deprecated: [dim]{count}[/]")

    console.print(Panel(
        f"[bold]Snapshot: {snapshot_dir.name}[/]\n"
        f"Total filters: {len(merged)}\n"
        + "  ".join(summary_parts),
        title="Snapshot View",
    ))

    STATUS_STYLE = {
        FilterStatus.ENABLED: "[green]enabled[/]",
        FilterStatus.DISABLED: "[yellow]disabled[/]",
        FilterStatus.ARCHIVED: "[cyan]archived[/]",
        FilterStatus.DEPRECATED: "[dim]deprecated[/]",
    }

    table = Table(title="Filters")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name", max_width=40)
    table.add_column("Status", justify="center")
    table.add_column("Conditions", max_width=50)
    table.add_column("Actions", max_width=30)

    for i, f in enumerate(merged, 1):
        status_str = STATUS_STYLE.get(f.status, str(f.status.value))

        # Color the name based on status
        name_style = {
            FilterStatus.ENABLED: "green",
            FilterStatus.DISABLED: "yellow",
            FilterStatus.ARCHIVED: "cyan",
            FilterStatus.DEPRECATED: "dim",
        }.get(f.status, "")
        name_str = f"[{name_style}]{f.name}[/]" if name_style else f.name

        cond_parts = []
        for c in f.conditions:
            cond_parts.append(f"{c.type.value} {c.operator.value} \"{c.value}\"")
        conds_str = f" {f.logic.value.upper()} ".join(cond_parts) if cond_parts else "[dim]none[/]"

        action_parts = []
        for a in f.actions:
            if a.parameters:
                params = ", ".join(f"{v}" for v in a.parameters.values())
                action_parts.append(f"{a.type.value}({params})")
            else:
                action_parts.append(a.type.value)
        actions_str = ", ".join(action_parts) if action_parts else "[dim]none[/]"

        table.add_row(str(i), name_str, status_str, conds_str, actions_str)

    console.print(table)


@snapshot_app.command("set-status")
def snapshot_set_status(
    name: str = typer.Argument(..., help="Filter name to update"),
    status: FilterStatus = typer.Argument(..., help="New status (enabled, disabled, archived, deprecated)"),
    backup_id: str = typer.Option("latest", "--backup", help="Backup identifier"),
):
    """Set the status of a filter in the archive.

    The backup.json file is immutable. Status overrides are stored in archive.json.
    """
    manager = BackupManager()
    snapshot_dir = manager.snapshot_dir_for(backup_id)
    bkup = manager.load_backup(backup_id)
    archive_entries = manager.load_archive(snapshot_dir)

    # Find the filter in archive or backup
    archive_idx = None
    for i, entry in enumerate(archive_entries):
        if entry.filter.name == name:
            archive_idx = i
            break

    backup_filter = None
    for f in bkup.filters:
        if f.name == name:
            backup_filter = f
            break

    if archive_idx is None and backup_filter is None:
        console.print(f"[red]Filter not found: '{name}'")
        raise typer.Exit(1)

    if archive_idx is not None:
        # Update existing archive entry
        archive_entries[archive_idx].filter.status = status
        archive_entries[archive_idx].filter.enabled = status == FilterStatus.ENABLED
        console.print(f"[green]Updated archive entry '{name}' -> {status.value}")
    else:
        # Create new archive entry from backup filter (backup stays immutable)
        archived_filter = backup_filter.model_copy(deep=True)
        archived_filter.status = status
        archived_filter.enabled = status == FilterStatus.ENABLED
        entry = ArchiveEntry(
            filter=archived_filter,
            archived_at=datetime.now(timezone.utc).isoformat(),
            source_snapshot=snapshot_dir.name,
        )
        archive_entries.append(entry)
        console.print(f"[green]Created archive entry '{name}' -> {status.value}")

    manager.write_archive(snapshot_dir, archive_entries)


@snapshot_app.command("remove")
def snapshot_remove(
    name: str = typer.Argument(..., help="Filter name to remove from archive"),
    backup_id: str = typer.Option("latest", "--backup", help="Backup identifier"),
):
    """Permanently remove a rule from the archive.

    To exclude a scraped filter from consolidation, use 'set-status <name> deprecated' instead.
    """
    manager = BackupManager()
    snapshot_dir = manager.snapshot_dir_for(backup_id)
    archive_entries = manager.load_archive(snapshot_dir)

    # Check if filter exists in archive
    found = False
    new_entries = []
    for entry in archive_entries:
        if entry.filter.name == name:
            found = True
        else:
            new_entries.append(entry)

    if not found:
        # Check if it's in backup only
        bkup = manager.load_backup(backup_id)
        in_backup = any(f.name == name for f in bkup.filters)
        if in_backup:
            console.print(f"[red]Filter '{name}' exists only in backup.json (immutable).")
            console.print("[yellow]Use 'snapshot set-status \"{name}\" deprecated' to exclude it from consolidation.")
        else:
            console.print(f"[red]Filter not found: '{name}'")
        raise typer.Exit(1)

    manager.write_archive(snapshot_dir, new_entries)
    console.print(f"[green]Removed '{name}' from archive ({len(archive_entries) - len(new_entries)} entries removed)")


def _refuse_damaged_backups(command):
    """Wrap a command so a failed backup checksum ends it with a clear message.

    load_backup raises BackupIntegrityError from many commands; this turns
    it into the message and exit 1 in one place, instead of a traceback.
    """
    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except BackupIntegrityError as e:
            console.print(f"[bold red]{escape(str(e))}")
            raise typer.Exit(1)
    return wrapper


for _typer_app in (app, snapshot_app):
    for _command_info in _typer_app.registered_commands:
        _command_info.callback = _refuse_damaged_backups(_command_info.callback)


if __name__ == "__main__":
    app()
