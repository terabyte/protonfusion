"""CLI entry point for ProtonFusion."""

import asyncio
import difflib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from src.utils.config import (
    load_credentials, SNAPSHOTS_DIR, TOOL_VERSION,
)
from src.models.filter_models import ProtonMailFilter, FilterStatus
from src.models.backup_models import Backup, ArchiveEntry
from src.backup.backup_manager import BackupManager
from src.backup.diff_engine import DiffEngine
from src.parser.filter_parser import parse_scraped_filters
from src.consolidator.consolidation_engine import ConsolidationEngine
from src.generator.sieve_generator import SieveGenerator, SECTION_BEGIN

SIEVE_FILTER_NAME = "ProtonFusion Consolidated"


app = typer.Typer(
    name="protonfusion",
    help="ProtonFusion - safely consolidate your ProtonMail filters into Sieve scripts.",
    add_completion=False,
)
snapshot_app = typer.Typer(help="Manage snapshot contents.")
app.add_typer(snapshot_app, name="snapshot")
console = Console()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _get_credentials(credentials_file: str, manual_login: bool):
    """Load credentials if applicable."""
    if manual_login:
        return None
    if credentials_file:
        return load_credentials(credentials_file)
    return None


@app.command()
def backup(
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Path to credentials file"),
    manual_login: bool = typer.Option(False, "--manual-login", help="Force manual login"),
    output: str = typer.Option("", "--output", help="Custom output path for backup file"),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
):
    """Scrape current filters and save to a timestamped snapshot."""
    from src.scraper.protonmail_scraper import ProtonMailScraper

    creds = _get_credentials(credentials_file, manual_login)
    workers = max(1, min(workers, 10))

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds)
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

            with console.status("[bold green]Reading existing Sieve script..."):
                sieve_script = await scraper.read_sieve_script(
                    filter_name=SIEVE_FILTER_NAME,
                )

            # Parse filters
            filters = parse_scraped_filters(raw_filters)

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

    asyncio.run(_run())


@app.command()
def show(
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Path to credentials file"),
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
        scraper = ProtonMailScraper(headless=headless, credentials=creds)
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

    asyncio.run(_run())


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

        table.add_row(str(i), f.name, status, conds_str, actions_str)

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
):
    """Generate optimized Sieve script from backup (local only, no ProtonMail changes)."""
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
    consolidated, report = engine.consolidate(
        backup_filters,
        include_disabled=include_disabled,
        synced_filter_hashes=synced_filter_hashes,
        archived_filters=archived_filters,
        exclude_names=exclude_names,
    )

    generator = SieveGenerator()
    sieve_script = generator.generate(consolidated)

    if output_file:
        out_path = Path(output_file)
    else:
        out_path = snapshot_dir / "consolidated.sieve"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(sieve_script)
    console.print(f"[green]Sieve script saved to: {out_path}")

    # Collect all processed filters and write manifest into snapshot dir
    all_source_names = set()
    for cf in consolidated:
        all_source_names.update(cf.source_filters)
    all_processed = backup_filters + archived_filters
    processed_filters = [f for f in all_processed if f.name in all_source_names]
    manager.write_manifest(snapshot_dir, processed_filters, str(out_path))
    console.print(f"[cyan]Manifest written to snapshot ({len(processed_filters)} filters)")

    # Post-consolidation archiving: move included backup filters to archive
    now_ts = datetime.now(timezone.utc).isoformat()
    for f in bkup.filters:
        if f.name in all_source_names and f.content_hash not in archive_by_hash:
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
        "created_at": now_ts,
    }
    (snapshot_dir / "consolidation_args.json").write_text(json.dumps(args_data, indent=2))

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
            scraper = ProtonMailScraper(headless=headless, credentials=creds)
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

        asyncio.run(_run())
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


@app.command()
def sync(
    sieve_file: str = typer.Option("", "--sieve", help="Path to Sieve script to upload (default: from snapshot)"),
    backup_id: str = typer.Option("latest", "--backup", help="Backup to reference for disabling filters"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without applying"),
    show_diff_only: bool = typer.Option(False, "--show-diff-only", help="Log in, fetch live Sieve, show diff, change nothing"),
):
    """Upload Sieve script and disable old UI filters (reversible)."""
    from src.scraper.protonmail_sync import ProtonMailSync

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

    if dry_run:
        console.print(Panel("[bold yellow]DRY RUN - No changes will be made"))
        console.print(f"\nWould upload Sieve script ({len(sieve_script)} chars)")
        console.print(f"Would disable {bkup.metadata.enabled_count} UI filters")

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
        return

    if show_diff_only:
        async def _show_diff():
            sync_client = ProtonMailSync(headless=headless, credentials=creds)
            try:
                await sync_client.initialize()
                await sync_client.login()
                await sync_client.navigate_to_filters()

                with console.status("[bold green]Reading existing Sieve script..."):
                    existing_script = await sync_client.read_sieve_script(
                        filter_name=SIEVE_FILTER_NAME,
                    )

                if not existing_script:
                    existing_script = ""

                merged_script = SieveGenerator.merge_with_existing(sieve_script, existing_script)

                if existing_script == merged_script:
                    console.print(Panel("[bold green]No changes — live script already matches."))
                    return

                diff_lines = list(difflib.unified_diff(
                    existing_script.splitlines(keepends=True),
                    merged_script.splitlines(keepends=True),
                    fromfile="live (ProtonMail)",
                    tofile="merged (would upload)",
                ))

                if not diff_lines:
                    console.print(Panel("[bold green]No changes — live script already matches."))
                    return

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
            finally:
                await sync_client.close()

        asyncio.run(_show_diff())
        return

    async def _run():
        sync_client = ProtonMailSync(headless=headless, credentials=creds)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()

            # Read existing script and merge
            with console.status("[bold green]Reading existing Sieve script..."):
                existing_script = await sync_client.read_sieve_script(
                    filter_name=SIEVE_FILTER_NAME,
                )

            if existing_script:
                console.print(f"[cyan]Found existing Sieve script ({len(existing_script)} chars)")
                merged_script = SieveGenerator.merge_with_existing(sieve_script, existing_script)
                if SECTION_BEGIN not in existing_script:
                    console.print("[yellow]User rules detected — preserving outside ProtonFusion section")
            else:
                merged_script = SieveGenerator.merge_with_existing(sieve_script, "")

            # Disable UI filters first to free up filter slots (ProtonMail
            # enforces a per-plan limit on active filters, so uploading a new
            # Sieve filter will fail if we're already at the limit).
            console.print("[bold green]Disabling old UI filters...")
            disabled = await sync_client.disable_all_ui_filters()
            console.print(f"[green]Disabled {disabled} filters")

            console.print("[bold green]Uploading merged Sieve script...")
            success = await sync_client.upload_sieve(
                merged_script, filter_name=SIEVE_FILTER_NAME,
            )
            if success:
                console.print("[green]Sieve script uploaded successfully!")
            else:
                console.print(
                    "[red]Failed to upload Sieve script. "
                    f"{disabled} UI filters were disabled.\n"
                    f"[yellow]To re-enable them, run: restore --backup {backup_id}"
                )
                return

            if manager.promote_manifest(snapshot_dir):
                console.print("[cyan]Sync manifest updated")

            console.print(Panel(
                f"[bold green]Sync complete![/]\n\n"
                f"Sieve uploaded: Yes\n"
                f"Filters disabled: {disabled}\n\n"
                f"[yellow]To rollback, run: restore --backup {backup_id}",
                title="Sync Complete",
            ))
        finally:
            await sync_client.close()

    asyncio.run(_run())


@app.command()
def restore(
    backup_id: str = typer.Option(..., "--backup", help="Backup to restore from"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
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
        scraper = ProtonMailScraper(headless=headless, credentials=creds)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            raw_filters = await scraper.scrape_all_filters(workers=_workers)
            current_filters = parse_scraped_filters(raw_filters)
        finally:
            await scraper.close()

        sync_client = ProtonMailSync(headless=headless, credentials=creds)
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

    asyncio.run(_run())


@app.command()
def cleanup(
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview what will be deleted"),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel browser tabs for scraping (1=sequential, max 10)"),
):
    """Delete all disabled filters (with confirmation).

    Auto-archives disabled filters before deletion to preserve them for future consolidation.
    """
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync

    creds = _get_credentials(credentials_file, False)
    _workers = max(1, min(workers, 10))
    manager = BackupManager()

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            raw_filters = await scraper.scrape_all_filters(workers=_workers)
            filters = parse_scraped_filters(raw_filters)
        finally:
            await scraper.close()

        disabled = [f for f in filters if not f.enabled]

        if not disabled:
            console.print("[green]No disabled filters to clean up.")
            return

        # Auto-archive any disabled filters missing from the archive
        try:
            latest_dir = manager.snapshot_dir_for("latest")
            archive_entries = manager.load_archive(latest_dir)
            archive_hashes = {e.filter.content_hash for e in archive_entries}
            now_ts = datetime.now(timezone.utc).isoformat()
            auto_archived = 0
            for f in disabled:
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
            console.print(f"  [yellow]- {f.name}")

        if dry_run:
            console.print("\n[bold yellow]DRY RUN - No filters will be deleted.")
            return

        confirm = typer.confirm(f"\nDelete {len(disabled)} disabled filters? This cannot be undone!")
        if not confirm:
            console.print("[yellow]Cleanup cancelled.")
            return

        sync_client = ProtonMailSync(headless=headless, credentials=creds)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()

            deleted_count = 0
            for f in disabled:
                if await sync_client.delete_filter(f.name):
                    deleted_count += 1
                    console.print(f"  [red]Deleted: {f.name}")

            console.print(f"\n[green]Deleted {deleted_count}/{len(disabled)} filters")
        finally:
            await sync_client.close()

    asyncio.run(_run())


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


if __name__ == "__main__":
    app()
