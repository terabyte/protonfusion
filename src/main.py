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
    load_credentials, loggable_text, SNAPSHOTS_DIR, TOOL_VERSION,
)
from src.models.filter_models import ProtonMailFilter, FilterStatus
from src.models.backup_models import Backup, ArchiveEntry
from src.backup.backup_manager import (
    BackupManager, BackupIntegrityError, predates_strict_parser, unverified_for_deletion,
)
from src.backup.diff_engine import DiffEngine
from src.backup.sync_plan import (
    DisablePlan, carried_hashes, check_disable_candidates, incomplete_in_script,
    incompleteness_reasons, plan_disable,
)
from src.utils.private_files import write_private_file
from src.parser.filter_parser import parse_scraped_filters
from src.consolidator.consolidation_engine import ConsolidationEngine
from src.generator.sieve_generator import SieveGenerator, SieveGenerationError, SECTION_BEGIN
from src.generator.sieve_rules import (
    SieveParseError, compare_sections, extract_section, script_facts, validate_script,
)
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


def _warn_if_old_snapshot(bkup: Backup, backup_id: str) -> bool:
    """Print a prominent warning if the backup predates the strict parser.

    Returns True when it does. Nothing in an old backup shows which
    conditions were misread, so the only fix is a fresh `backup`.
    """
    if not predates_strict_parser(bkup):
        return False
    console.print(Panel(
        f"[bold red]Backup '{escape(backup_id)}' is format {escape(bkup.version)}, written by an older "
        "ProtonFusion that misread some operators:[/]\n"
        '  "is not" was stored as "is", "does not contain" as "contains", and "begins with" '
        'or "ends with" as "contains".\n'
        "A script built from it can match different (often more) mail than your filters do, and "
        "nothing in the file shows which conditions were misread.\n\n"
        "[bold]Run 'backup' again, then 'consolidate', before syncing.[/]",
        title="Old Snapshot", border_style="red",
    ))
    return True


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
    show_raw: bool = typer.Option(
        False, "--show-raw",
        help="Also print each filter's raw scrape evidence (the wizard text or Sieve script) and scrape issues",
    ),
):
    """Display filters from a backup file (offline, no login needed).

    With --show-raw, also prints what the scraper saw for each filter, so a
    field the parser missed or misread can be recovered by hand.
    """
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)
    _display_filters(bkup.filters, source=f"backup '{backup_id}'")
    if show_raw:
        _display_raw_evidence(bkup.filters)
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
            cond_parts.append(f"{c.type.value} {c.operator.value} {escape(c.display_value)}")
        conds_str = f" {f.logic.value.upper()} ".join(cond_parts) if cond_parts else "[dim]none[/]"

        # Format actions
        action_parts = []
        for a in f.actions:
            if a.parameters:
                params = ", ".join(escape(f"{v}") for v in a.parameters.values())
                action_parts.append(f"{a.type.value}({params})")
            else:
                action_parts.append(a.type.value)
        actions_str = ", ".join(action_parts) if action_parts else "[dim]none[/]"

        name_str = escape(f.name) if f.is_complete else f"{escape(f.name)} [red](incomplete)[/]"
        table.add_row(str(i), name_str, status, conds_str, actions_str)

    console.print(table)


def _display_raw_evidence(filters: list) -> None:
    """Print each filter's raw scrape evidence and scrape issues, numbered as in the table.

    This is the recovery path the raw text exists for: what the wizard (or
    the Sieve editor) showed when the filter was backed up, verbatim.
    """
    console.print("\n[bold]Raw scrape evidence[/]")
    for i, f in enumerate(filters, 1):
        lines = []
        if f.scrape_issues:
            lines.append("[red]Scrape issues:[/]")
            lines.extend(f"  [red]- {escape(issue)}[/]" for issue in f.scrape_issues)
        if f.raw is None:
            lines.append("[yellow]No raw evidence (backed up before format 1.1).[/]")
        else:
            for label, text in (
                ("Conditions text", f.raw.conditions_text),
                ("Actions text", f.raw.actions_text),
                ("Sieve script", f.raw.sieve_text),
            ):
                if text:
                    lines.append(f"[cyan]{label}:[/]")
                    lines.append(escape(text))
            if not (f.raw.conditions_text or f.raw.actions_text or f.raw.sieve_text):
                lines.append("[dim]Raw evidence is empty.[/]")
        console.print(Panel("\n".join(lines), title=f"#{i} {escape(f.name)}", title_align="left"))


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
    _warn_if_old_snapshot(bkup, backup_id)

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
    # Absolute, so sync matches the manifest to this script from any
    # working directory (a relative --output would only match from here).
    manager.write_manifest(
        snapshot_dir, processed_filters, str(out_path.resolve()),
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
    if result.folder_fixes:
        fix_lines = [
            "",
            f"[yellow]Corrected: {len(result.folder_fixes)} Trash/Spam/Inbox actions.[/]",
            "Older ProtonFusion versions wrote 'Move to Trash' as discard (a permanent "
            "delete) and Spam/Inbox under their dropdown labels. The new section uses "
            'fileinto "trash" / "spam" / "inbox"; these are not dropped rules.',
        ]
        for old, new in result.folder_fixes:
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
        (plan.not_covered, "bold red",
         f"have rules that are not all in the script being uploaded, so they {leave} enabled. "
         "The script may come from another consolidate run; re-run 'consolidate' for this backup."),
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


async def _reenable_after_failed_upload(
    sync_client, disabled: List[ProtonMailFilter], backup_id: str,
    expected_names: Optional[List[str]] = None,
) -> None:
    """Turn back on every filter this sync disabled, after its upload failed.

    Otherwise a failed upload leaves neither the old UI filters nor the new
    script handling mail. Every filter is attempted even if an earlier one
    raised, and each one that is not confirmed back on is listed with why,
    plus the `restore` command that brings it back.
    """
    if not disabled:
        console.print("[yellow]No filters had been disabled, so nothing else changed.")
        return
    try:
        # The failed upload may have left the Sieve editor open over the list
        await sync_client.navigate_to_filters()
    except Exception as e:
        logger.warning("Could not reload the filters page before re-enabling: %s", loggable_text(str(e)))

    failed: List[tuple] = []  # (filter, reason)
    for f in disabled:
        try:
            if await sync_client.set_row_enabled(f.priority, f.name, True, expected_names=expected_names):
                continue
            reason = "its row could not be identified with certainty"
        except Exception as e:
            reason = loggable_text(str(e)) or type(e).__name__
            logger.warning("Re-enabling '%s' failed: %s", f.name, reason)
        failed.append((f, reason))

    reenabled = len(disabled) - len(failed)
    color = "green" if not failed else "yellow"
    console.print(f"[{color}]Re-enabled {reenabled} of the {len(disabled)} filters this sync disabled.")
    if failed:
        console.print(f"[bold red]Could not re-enable {len(failed)} filter(s); they are still disabled:")
        for f, reason in failed:
            console.print(f"  [red]- {escape(f.name)}: {escape(reason)}")
        console.print(f"[yellow]To re-enable them, run: restore --backup {backup_id}")


def _scraped_row_names(live_filters: List[ProtonMailFilter]) -> Optional[List[str]]:
    """Every scraped row's name in list order, or None if the scrape has gaps.

    Lets set_row_enabled check the live list is the one scraped before it
    trusts a stored row position. If any row did not make it into
    `live_filters` (its priorities are not exactly 0..n-1) there is no full
    list to compare, so positions fall back to unique-name matching.
    """
    by_priority = sorted(live_filters, key=lambda f: f.priority)
    if [f.priority for f in by_priority] != list(range(len(by_priority))):
        return None
    return [f.name for f in by_priority]


def _uploaded_facts(merged_script: str) -> set:
    """Facts of the ProtonFusion section about to be uploaded (empty if it does not parse).

    plan_disable checks every filter against these before disabling it, so
    an unparsable section means nothing is disabled.
    """
    try:
        return script_facts(merged_script)
    except SieveParseError as e:
        logger.warning("Could not parse the script being uploaded: %s", e)
        return set()


def _merged_script_problem(script: str) -> Optional[str]:
    """Why the merged script is not valid Sieve, or None if it is.

    sieve_rules.validate_script is the check: the script must parse
    (including multi-line text: literals), every require must come before
    any other command (RFC 5228 section 3.2), and the extensions
    ProtonFusion's own commands need must be required.
    """
    try:
        validate_script(script)
    except SieveParseError as e:
        return str(e)
    return None


def _merge_for_upload(new_script: str, existing_script: str) -> tuple[Optional[str], Optional[str]]:
    """Merge the new script into the existing one: (merged, None), or (None, why not).

    merge_with_existing raises SieveParseError when either script does not
    parse (usually the user's rules outside the ProtonFusion section), and
    a merge that succeeds is still validated, so every caller gets one
    answer to "can this be uploaded?" and refuses the same way.
    """
    try:
        merged = SieveGenerator.merge_with_existing(new_script, existing_script)
    except SieveParseError as e:
        return None, f"{e} (in the existing script or the new one, so they could not be merged)"
    return merged, _merged_script_problem(merged)


def _report_merged_script_problem(problem: Optional[str]) -> bool:
    """Print why the merged script cannot be uploaded; True if there is no problem."""
    if problem is None:
        return True
    console.print(Panel(
        f"[bold red]The merged Sieve script does not parse: {escape(problem)}[/]\n"
        "Uploading it could leave the account with a broken or partly applied script. "
        "Check the rules outside the ProtonFusion section in the live script.",
        title="Script Validation", border_style="red",
    ))
    return False


def _refuse_incomplete_sources(incomplete: List[ProtonMailFilter], allow_incomplete: bool) -> None:
    """Refuse the sync (exit 1) when the script holds rules from incomplete filters.

    Lists each filter with why it is incomplete. --allow-incomplete turns
    the refusal into a warning.
    """
    if not incomplete:
        return
    console.print(
        f"[bold red]This script holds rules from {len(incomplete)} filter(s) that were not read in "
        "full; their rules may be wider or narrower than the real filters, or missing labels:"
    )
    for f in incomplete:
        console.print(f"  [red]- {escape(f.name)}")
        for reason in incompleteness_reasons(f):
            console.print(f"      {escape(reason)}")
    if allow_incomplete:
        console.print("[yellow]--allow-incomplete given: proceeding anyway.")
        return
    console.print(
        "[bold red]Sync refused. No filters were disabled and nothing was uploaded.[/]\n"
        "[yellow]Run 'backup' and 'consolidate' again, or pass --allow-incomplete."
    )
    raise typer.Exit(1)


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
        help="Upload even if the script holds rules from filters that were not read in full "
             "(scrape issues, or no raw evidence from a pre-1.1 backup)",
    ),
    allow_old_snapshot: bool = typer.Option(
        False, "--allow-old-snapshot",
        help="Sync from a backup written before the strict parser (format < 1.3), which may hold misread operators",
    ),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
):
    """Upload Sieve script and disable the UI filters it replaces (reversible).

    Only wizard filters whose rules are in the script (same content as a
    filter 'consolidate' put into it) are disabled. Sieve filters, filters
    created or changed after the backup, and filters that cannot be read in
    full stay enabled. If the upload fails, every filter this run disabled
    is enabled again. When the live script already matches (ignoring
    trailing whitespace) nothing is uploaded; the filters are still
    disabled and ProtonFusion's Sieve filter is switched on if it is off.

    Refuses (exit 1, nothing changed) if the new ProtonFusion section would drop
    any rule present in the live section, unless --allow-rule-removal is given.
    Also refuses if the script holds rules from filters in the backup or
    archive that were not read in full (scrape issues, or no raw evidence
    because they were backed up before format 1.1), unless --allow-incomplete
    is given.
    It also refuses a backup that predates the strict parser (format before
    1.3, which may hold misread operators), unless --allow-old-snapshot is given.
    """
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync, normalize_script

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

    if _warn_if_old_snapshot(bkup, backup_id):
        if allow_old_snapshot:
            console.print("[yellow]--allow-old-snapshot given: proceeding anyway.")
        else:
            console.print(
                "[bold red]Sync refused. No filters were disabled and nothing was uploaded.[/]\n"
                "[yellow]Run 'backup' and 'consolidate' again, or pass --allow-old-snapshot."
            )
            raise typer.Exit(1)

    manifest = manager.load_manifest(snapshot_dir) or {}
    # Which UI filters the script replaces, by content. `reference` adds the
    # archive so a filter the script leaves out on purpose (deprecated) is
    # reported as such rather than as new since the backup.
    carried, from_manifest = carried_hashes(manifest, sieve_path, bkup.filters)
    reference = list(bkup.filters) + [e.filter for e in manager.load_archive(snapshot_dir)]

    # A rule taken from a filter the scraper could not fully read (or one
    # backed up before format 1.1, which may be missing its labels) may be
    # wider or narrower than the real filter. Checked against the script
    # itself, from the backup and archive, so it holds for any script.
    _refuse_incomplete_sources(
        incomplete_in_script(
            reference, _uploaded_facts(sieve_script),
            set(manifest.get("filter_hashes", [])) if from_manifest else set(),
        ),
        allow_incomplete,
    )

    if dry_run:
        console.print(Panel("[bold yellow]DRY RUN - No changes will be made"))
        console.print(f"\nWould upload Sieve script ({len(sieve_script)} chars)")
        _print_carried_source(from_manifest, backup_id)
        backed_up_merge, merge_problem = _merge_for_upload(sieve_script, bkup.sieve_script or "")
        _print_disable_plan(
            plan_disable(
                bkup.filters, carried, reference, SIEVE_FILTER_NAME, _uploaded_facts(backed_up_merge or ""),
            ),
            backup_id, preview=True,
        )
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
        if bkup.sieve_script and backed_up_merge is not None:
            merged = backed_up_merge
            console.print(f"\n[cyan]Existing Sieve script in backup: {len(bkup.sieve_script)} chars")
            if SECTION_BEGIN not in bkup.sieve_script:
                console.print("[yellow]User rules detected — will be preserved outside ProtonFusion section")
            if len(merged) < 3000:
                console.print(Panel(merged, title="Merged Script Preview", border_style="cyan"))
            else:
                preview = "\n".join(merged.split("\n")[:40])
                console.print(Panel(preview + "\n...", title="Merged Script Preview (first 40 lines)", border_style="cyan"))
        if not _report_merged_script_problem(merge_problem):
            safe = False
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
            merged_script, merge_problem = _merge_for_upload(sieve_script, existing_script)
            if not _report_merged_script_problem(merge_problem):
                safe = False
            if merged_script is None:
                # Nothing to diff against: the scripts could not be merged.
                console.print("[bold red]A real sync would REFUSE and change nothing.")
                return False
            if not safe:
                console.print("[bold red]A real sync would REFUSE and change nothing.")
            else:
                _print_carried_source(from_manifest, backup_id)
                _print_disable_plan(
                    plan_disable(
                        live_filters, carried, reference, SIEVE_FILTER_NAME, _uploaded_facts(merged_script),
                    ),
                    backup_id, preview=True,
                )

            if normalize_script(existing_script) == normalize_script(merged_script):
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

        merged_script, merge_problem = _merge_for_upload(sieve_script, existing_script)
        if not _report_merged_script_problem(merge_problem):
            console.print("[bold red]Sync refused. No filters were disabled and nothing was uploaded.")
            return False
        if existing_script and SECTION_BEGIN not in existing_script:
            console.print("[yellow]User rules detected; preserving them outside ProtonFusion section")

        # Nothing to upload when the live script already is the merged one:
        # Proton keeps Save disabled for an unchanged script, so upload_sieve
        # would report a failure. The script's filter must still be on before
        # the UI filters go off, so it is located now (refusing if it cannot
        # be) and switched on in place of the upload if needed.
        unchanged = bool(existing_script) and normalize_script(merged_script) == normalize_script(existing_script)
        live_pf = None
        if unchanged:
            pf_rows = [f for f in live_filters if f.is_sieve and f.name == SIEVE_FILTER_NAME]
            if len(pf_rows) != 1:
                console.print(
                    f"[bold red]The live script already matches, but {len(pf_rows)} Sieve filters named "
                    f"'{SIEVE_FILTER_NAME}' were read, so there is no telling whether the one holding it "
                    "is switched on.[/]\n"
                    "[bold red]Sync refused. No filters were disabled and nothing was uploaded."
                )
                return False
            live_pf = pf_rows[0]

        _print_carried_source(from_manifest, backup_id)
        plan = plan_disable(live_filters, carried, reference, SIEVE_FILTER_NAME, _uploaded_facts(merged_script))
        _print_disable_plan(plan, backup_id, preview=False)

        sync_client = ProtonMailSync(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()

            # Disable the replaced filters first: ProtonMail limits active
            # filters per plan, so a new Sieve filter can fail to save while
            # they are on. Rows are found by scraped position and name.
            check_disable_candidates(plan.to_disable)
            expected_names = _scraped_row_names(live_filters)
            disabled: List[ProtonMailFilter] = []
            not_disabled: List[ProtonMailFilter] = []
            for f in plan.to_disable:
                try:
                    # require_current: a row the user switched off since the
                    # scrape is left alone and never joins `disabled`.
                    ok = await sync_client.set_row_enabled(
                        f.priority, f.name, False,
                        expected_names=expected_names, require_current=True,
                    )
                except Exception as e:
                    # The row's state is unknown. It was enabled when scraped,
                    # so re-enabling it with the rest restores the start state.
                    console.print(
                        f"[bold red]Sync stopped while disabling '{escape(f.name)}' "
                        f"({escape(loggable_text(str(e)))}). Nothing was uploaded."
                    )
                    await _reenable_after_failed_upload(
                        sync_client, disabled + [f], backup_id, expected_names,
                    )
                    return False
                if ok:
                    disabled.append(f)
                else:
                    not_disabled.append(f)
            console.print(f"[green]Disabled {len(disabled)} filters")
            if not_disabled:
                console.print(
                    f"[yellow]Could not disable {len(not_disabled)} filter(s): the row could not be "
                    "identified with certainty, or was already switched off since the scrape. "
                    "Any still enabled stay enabled alongside the Sieve script:"
                )
                for f in not_disabled:
                    console.print(f"  [yellow]- {escape(f.name)}")

            upload_error = None
            try:
                if unchanged:
                    console.print("[green]The live Sieve script already matches the merged one; nothing to upload.")
                    # Same end state an upload guarantees: the script's filter is on.
                    success = live_pf.enabled or await sync_client.set_row_enabled(
                        live_pf.priority, live_pf.name, True, expected_names=expected_names,
                    )
                else:
                    console.print("[bold green]Uploading merged Sieve script...")
                    success = await sync_client.upload_sieve(merged_script, filter_name=SIEVE_FILTER_NAME)
            except Exception as e:
                success = False
                upload_error = e

            if not success:
                reason = f" ({escape(loggable_text(str(upload_error)))})" if upload_error else ""
                if unchanged:
                    console.print(f"[bold red]Failed to switch on the '{SIEVE_FILTER_NAME}' filter{reason}.")
                else:
                    console.print(f"[bold red]Failed to upload Sieve script{reason}.")
                if sync_client.upload_hit_filter_limit:
                    console.print(
                        "[yellow]The 'Add sieve filter' button was missing, which is how ProtonMail "
                        "shows an account at its active-filter limit. The filters left enabled above "
                        "count toward it. Disable or delete enough of them by hand (or fold them in "
                        "with 'backup' and 'consolidate'), then re-run sync."
                    )
                await _reenable_after_failed_upload(sync_client, disabled, backup_id, expected_names)
                return False

            if not unchanged:
                console.print("[green]Sieve script uploaded successfully!")
            if manager.promote_manifest(snapshot_dir):
                console.print("[cyan]Sync manifest updated")

            console.print(Panel(
                f"[bold green]Sync complete![/]\n\n"
                f"Sieve uploaded: {'No (already up to date)' if unchanged else 'Yes'}\n"
                f"Filters disabled: {len(disabled)}\n"
                f"Filters left enabled: {len(plan.left_enabled) + len(not_disabled)}\n\n"
                + _rollback_help(backup_id, snapshot_dir),
                title="Sync Complete",
            ))
            return True
        finally:
            await sync_client.close()

    if not _run_browser_command(_run()):
        raise typer.Exit(1)


def _rollback_help(backup_id: str, snapshot_dir: Path) -> str:
    """What to tell the user about undoing a sync."""
    return (
        f"[yellow]To roll back: 'restore --backup {escape(backup_id)}' puts back both the UI "
        "filters' on/off states and the Sieve script captured in that backup "
        f"({escape(str(snapshot_dir / 'backup.json'))}), after a preview and confirmation, "
        "and saves a safety backup first."
    )


def _section_rules(script: str) -> Optional[set]:
    """Rule facts of a script's ProtonFusion section; None if it has no section.

    Raises SieveParseError if the section cannot be parsed.
    """
    if extract_section(script or "") is None:
        return None
    return script_facts(script)


def _print_restore_script_preview(live_script: str, target_script: str, backup_id: str) -> None:
    """Show how restoring would change the live script: a unified diff plus the
    effect on ProtonFusion's section, in rules."""
    try:
        live_rules = _section_rules(live_script)
        target_rules = _section_rules(target_script)
    except SieveParseError as e:
        live_rules = target_rules = None
        console.print(f"[yellow]Could not compare the ProtonFusion sections rule by rule: {escape(str(e))}")
    else:
        if live_rules is not None and target_rules is None:
            console.print(
                f"[bold yellow]The backed-up script has no ProtonFusion section (the backup predates "
                f"ProtonFusion's Sieve filter or its first sync): restoring it REMOVES ProtonFusion's "
                f"section ({len(live_rules)} condition/action pairs) from the live script.[/]"
            )
        elif live_rules is not None and target_rules is not None:
            console.print(
                f"[cyan]ProtonFusion section: {len(live_rules - target_rules)} condition/action pairs "
                f"removed, {len(target_rules - live_rules)} added.[/]"
            )
    console.print(
        f"[cyan]The whole script of the '{SIEVE_FILTER_NAME}' filter is replaced, including any "
        "text outside the ProtonFusion markers.[/]"
    )
    diff_lines = list(difflib.unified_diff(
        live_script.splitlines(), target_script.splitlines(),
        fromfile="live", tofile=f"backup {backup_id}", lineterm="",
    ))
    shown = "\n".join(diff_lines[:200]) + ("\n..." if len(diff_lines) > 200 else "")
    console.print(Panel(escape(shown), title="Sieve script: live -> backup", border_style="cyan"))


def _print_restore_filter_preview(plan) -> None:
    """List the filter toggles a restore plan would make, and what it cannot restore."""
    for pairs, verb, color in ((plan.to_enable, "enable", "green"), (plan.to_disable, "disable", "yellow")):
        if pairs:
            console.print(f"[{color}]Will {verb} {len(pairs)} filter(s):")
            for backed, live in pairs:
                console.print(f"  [{color}]- {escape(backed.name)} (row {live.priority})")
    console.print(f"[cyan]Already as in the backup: {len(plan.already_correct)}")
    if plan.unrestorable:
        console.print(f"[bold red]Cannot restore {len(plan.unrestorable)} filter(s); they are left alone:")
        for line in plan.unrestorable:
            console.print(f"  [red]- {escape(line)}")
    if plan.script_differs:
        console.print(
            "[yellow]Other Sieve filters whose script differs from the backup (the backup holds only "
            f"'{SIEVE_FILTER_NAME}'s script, so only their on/off state is restored):"
        )
        for name in plan.script_differs:
            console.print(f"  [yellow]- {escape(name)}")


@app.command()
def restore(
    backup_id: str = typer.Option(..., "--backup", help="Backup to restore from"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    state: str = typer.Option("", "--state", help=STATE_HELP),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change, change nothing"),
    allow_empty_script: bool = typer.Option(
        False, "--allow-empty-script",
        help=f"The backup holds no Sieve script but the account does: restore by disabling "
             f"'{SIEVE_FILTER_NAME}' (stops every rule in it)",
    ),
):
    """Roll the account back to a backup: UI filter states AND the ProtonFusion Sieve script.

    Shows a preview (each filter it will enable or disable, and a diff of
    the live script against the backed-up one), asks for confirmation, and
    saves a safety backup of the current state first, printing its id so
    the restore can itself be undone. --dry-run stops after the preview.

    Filters are matched by content (Sieve filters by name) and toggled by
    row position plus name, so a shared name never toggles the wrong one;
    any it cannot match unambiguously are listed and left alone (exit 1).

    Order, chosen so a failure part-way never leaves mail unfiltered: first
    enable the filters the backup has on, then replace the script, then
    disable the filters the backup has off. Until the last step every rule
    from both the old and the current state is active, so a failure leaves
    extra filtering (possibly the same rule twice), never a rule off. It
    stops at the first failed enable or a failed upload and reports
    exactly what state the account is in.

    Refuses if the live script cannot be read, and if the backup holds no
    script while the account does, unless --allow-empty-script.
    """
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync, normalize_script
    from src.backup.restore_engine import RestoreEngine

    creds = _get_credentials(credentials_file, False)
    _workers = max(1, min(workers, 10))
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)

    async def _run() -> bool:
        # Read everything first, in one read-only session. A failed live
        # Sieve read raises SieveReadError, which _run_browser_command turns
        # into a refusal before anything is changed or saved.
        scraper = ProtonMailScraper(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            current_filters = parse_scraped_filters(await scraper.scrape_all_filters(workers=_workers))
            live_script = await scraper.read_sieve_script(filter_name=SIEVE_FILTER_NAME) or ""
            account_email = scraper.account_email
        finally:
            await scraper.close()

        plan = RestoreEngine.plan(bkup, current_filters)
        target_script = bkup.sieve_script or ""
        # ProtonFusion's own filter has its script restored, not just its state
        plan.script_differs = [n for n in plan.script_differs if n != SIEVE_FILTER_NAME]
        live_pf = [f for f in current_filters if f.is_sieve and f.name == SIEVE_FILTER_NAME]

        # What to do with the script: nothing, upload the backed-up one, or
        # (backup had none) disable ProtonFusion's filter.
        # Trailing whitespace is ignored: Proton keeps Save disabled for a
        # script that has not changed, so uploading it would fail.
        script_action = "none"
        if target_script and normalize_script(target_script) != normalize_script(live_script):
            script_action = "upload"
        elif not target_script and live_script:
            console.print(
                f"[bold red]Backup '{escape(backup_id)}' holds no Sieve script, but the account's "
                f"'{SIEVE_FILTER_NAME}' filter has one ({len(live_script)} chars).[/]\n"
                "Either the account had no ProtonFusion script when the backup was made, or the "
                "backup predates script capture. Restoring means switching that script off: "
                f"with --allow-empty-script, restore DISABLES the '{SIEVE_FILTER_NAME}' filter "
                "(it is not deleted, so it can be switched back on), which stops every rule in it."
            )
            if not allow_empty_script:
                console.print("[bold red]Restore refused. Nothing was changed.")
                return False
            if len(live_pf) != 1:
                console.print(
                    f"[bold red]Found {len(live_pf)} Sieve filters named '{SIEVE_FILTER_NAME}'; "
                    "cannot tell which to disable. Restore refused. Nothing was changed."
                )
                return False
            script_action = "disable"

        # Saving a script switches ProtonFusion's filter on (upload_sieve
        # makes sure of it), so if the backup has it off, switch it off
        # again in the disable step, after the upload.
        pf_disable_pairs = []
        if script_action == "upload":
            backed_pf = [f for f in bkup.filters if f.is_sieve and f.name == SIEVE_FILTER_NAME and not f.enabled]
            already = {backed.name for backed, _ in plan.to_disable}
            if backed_pf and len(live_pf) == 1 and SIEVE_FILTER_NAME not in already:
                pf_disable_pairs = [(backed_pf[0], live_pf[0])]
                plan.already_correct = [n for n in plan.already_correct if n != SIEVE_FILTER_NAME]
        elif script_action == "disable":
            plan.to_enable = [(b, l) for b, l in plan.to_enable if l.name != SIEVE_FILTER_NAME]
            if SIEVE_FILTER_NAME not in {l.name for _, l in plan.to_disable}:
                pf_disable_pairs = [(live_pf[0], live_pf[0])]
        to_disable = plan.to_disable + pf_disable_pairs

        # Preview
        console.print(Panel(f"[bold]Restore preview: backup '{escape(backup_id)}'[/]", border_style="cyan"))
        _print_restore_filter_preview(plan)
        if pf_disable_pairs and script_action == "upload":
            console.print(f"[yellow]'{SIEVE_FILTER_NAME}' is off in the backup: it is switched off after the upload.")
        if script_action == "upload":
            _print_restore_script_preview(live_script, target_script, backup_id)
        elif script_action == "disable":
            console.print(f"[bold yellow]Will DISABLE the '{SIEVE_FILTER_NAME}' filter (last step).")
        else:
            console.print("[cyan]Sieve script: already as in the backup, unchanged.")

        restorable_ok = not plan.unrestorable
        if not (plan.to_enable or to_disable or script_action != "none"):
            console.print("[green]Nothing to change: the account already matches the backup.")
            return restorable_ok
        if dry_run:
            console.print("\n[bold yellow]DRY RUN - nothing was changed and nothing was saved.")
            return restorable_ok
        if not typer.confirm("\nApply this restore?"):
            console.print("[yellow]Restore cancelled. Nothing was changed.")
            return restorable_ok

        # Safety backup of the state about to be changed. Not made 'latest':
        # after the restore it no longer describes the account.
        manager.create_backup(
            current_filters, account_email=account_email, sieve_script=live_script, make_latest=False,
        )
        safety_id = manager.last_snapshot_dir.name
        console.print(
            f"[cyan]Safety backup of the current state: {safety_id} "
            f"(undo this restore with: restore --backup {safety_id})"
        )

        engine = None
        enabled, enable_errors = [], []
        disabled, disable_errors = [], []
        script_status = "unchanged"
        stopped_at = None
        sync_client = ProtonMailSync(headless=headless, credentials=creds, storage_state_path=state or None)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()
            engine = RestoreEngine(sync_client)

            # 1. Enable: only adds filtering
            enabled, enable_errors = await engine.apply(plan.to_enable, True)
            if enable_errors:
                stopped_at = "enable"

            # 2. Replace the script
            if stopped_at is None and script_action == "upload":
                try:
                    uploaded = await sync_client.upload_sieve(target_script, filter_name=SIEVE_FILTER_NAME)
                except Exception as e:
                    uploaded = False
                    script_status = (
                        f"UNKNOWN: the upload raised an error ({loggable_text(str(e))}); the live script "
                        "may or may not have changed. Check the filter in ProtonMail."
                    )
                else:
                    script_status = "restored to the backed-up script" if uploaded else (
                        "unchanged: the upload did not complete"
                    )
                if not uploaded:
                    stopped_at = "upload"

            # 3. Disable: only removes filtering, so last
            if stopped_at is None:
                disabled, disable_errors = await engine.apply(to_disable, False)
                if script_action == "disable":
                    pf_off = SIEVE_FILTER_NAME in disabled
                    script_status = (
                        f"'{SIEVE_FILTER_NAME}' disabled" if pf_off
                        else f"unchanged: could not disable '{SIEVE_FILTER_NAME}'"
                    )
        finally:
            await sync_client.close()

        _print_restore_outcome(
            plan, to_disable, enabled, enable_errors, disabled, disable_errors,
            script_action, script_status, stopped_at, safety_id,
        )
        return restorable_ok and not (enable_errors or disable_errors or stopped_at)

    if not _run_browser_command(_run()):
        raise typer.Exit(1)


def _print_restore_outcome(
    plan, to_disable, enabled, enable_errors, disabled, disable_errors,
    script_action, script_status, stopped_at, safety_id,
) -> None:
    """Say exactly what state the account is in after a (possibly partial) restore."""
    complete = not (stopped_at or enable_errors or disable_errors or plan.unrestorable)
    heading = "[bold green]Restore complete.[/]" if complete else "[bold red]Restore did NOT complete.[/]"
    lines = [heading, ""]
    lines.append(f"Enabled: {len(enabled)} of {len(plan.to_enable)}")
    if script_action != "none":
        lines.append(f"Sieve script: {escape(script_status)}")
    if stopped_at:
        lines.append(f"Disabled: none of {len(to_disable)} (not attempted: stopped before this step)")
        lines.append(
            "[yellow]Every rule from before the restore is still active alongside anything "
            "re-enabled, so no mail is left unfiltered; some may be filtered twice.[/]"
        )
    else:
        lines.append(f"Disabled: {len(disabled)} of {len(to_disable)}")
    if plan.unrestorable:
        lines.append(f"Not restorable (left alone): {len(plan.unrestorable)}")
    lines.append(f"\nTo undo: restore --backup {safety_id}")
    if not complete:
        lines.append("To finish: fix the cause and run the same restore again (it only changes what still differs).")
    console.print(Panel("\n".join(lines), title="Restore Report"))
    for title, errors in (("Could not enable", enable_errors), ("Could not disable", disable_errors)):
        if errors:
            console.print(f"[bold red]{title}:")
            for line in errors:
                console.print(f"  [red]- {escape(line)}")


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
    whose name another filter shares, since deletion works by name.
    Also refuses to delete any filter without a complete backup copy in the
    latest snapshot, unless --allow-incomplete is given.

    Once the deletion is confirmed, and only then, each filter about to be
    deleted is added to the latest snapshot's archive.json: as ARCHIVED
    (kept in future scripts) if its rules are in the live section, or as
    DEPRECATED (kept for the record, never consolidated) if it was deleted
    with --include-uncovered. A dry run or a declined confirmation writes
    nothing.

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
        uncovered_ids = {id(f) for f in uncovered}
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
            console.print(
                f"\n[bold yellow]DRY RUN - No filters will be deleted ({len(to_delete)} would be) "
                "and nothing is written."
            )
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

        _archive_before_deletion(manager, to_delete, uncovered_ids)

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


def _archive_before_deletion(
    manager: BackupManager, to_delete: List[ProtonMailFilter], uncovered_ids: set,
) -> None:
    """Add each filter cleanup is about to delete to the latest archive.json.

    Called only after the deletion is confirmed, and only for the filters
    being deleted, so a dry run, a declined prompt or a refused filter
    leaves the archive untouched (archiving a refused filter's live copy
    would give the next run the backup copy this run found missing).

    A filter whose rules are in the live section is archived as ARCHIVED,
    so future scripts keep its rules. One deleted with --include-uncovered
    (id in uncovered_ids) is archived as DEPRECATED: its rules are not
    live, and it was disabled, so consolidating it would switch on a rule
    the user had switched off. It stays recoverable with
    'snapshot set-status'. A hash already in the archive is left as it is.
    """
    try:
        latest_dir = manager.snapshot_dir_for("latest")
    except FileNotFoundError:
        return  # No snapshot to archive into; deletion was verified without one
    archive_entries = manager.load_archive(latest_dir)
    archive_hashes = {e.filter.content_hash for e in archive_entries}
    now_ts = datetime.now(timezone.utc).isoformat()
    added = {FilterStatus.ARCHIVED: 0, FilterStatus.DEPRECATED: 0}
    for f in to_delete:
        if f.content_hash in archive_hashes:
            continue
        archive_hashes.add(f.content_hash)
        status = FilterStatus.DEPRECATED if id(f) in uncovered_ids else FilterStatus.ARCHIVED
        archived_f = f.model_copy(deep=True)
        archived_f.status = status
        archived_f.enabled = False
        archive_entries.append(ArchiveEntry(
            filter=archived_f, archived_at=now_ts, source_snapshot=latest_dir.name,
        ))
        added[status] += 1
    if any(added.values()):
        manager.write_archive(latest_dir, archive_entries)
        console.print(
            f"[cyan]Archived before deletion: {added[FilterStatus.ARCHIVED]} (rules live), "
            f"{added[FilterStatus.DEPRECATED]} deprecated (rules not live, kept for the record)"
        )


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
        name_str = f"[{name_style}]{escape(f.name)}[/]" if name_style else escape(f.name)

        cond_parts = []
        for c in f.conditions:
            cond_parts.append(f"{c.type.value} {c.operator.value} {escape(c.display_value)}")
        conds_str = f" {f.logic.value.upper()} ".join(cond_parts) if cond_parts else "[dim]none[/]"

        action_parts = []
        for a in f.actions:
            if a.parameters:
                params = ", ".join(escape(f"{v}") for v in a.parameters.values())
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
