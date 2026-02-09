"""CLI entry point for ProtonMail Filter Consolidation Tool."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from src.utils.config import (
    load_credentials, BACKUPS_DIR, OUTPUT_DIR, TOOL_VERSION,
)
from src.models.filter_models import ProtonMailFilter
from src.models.backup_models import Backup
from src.backup.backup_manager import BackupManager
from src.backup.diff_engine import DiffEngine
from src.parser.filter_parser import parse_scraped_filters
from src.consolidator.consolidation_engine import ConsolidationEngine
from src.generator.sieve_generator import SieveGenerator

app = typer.Typer(
    name="protonmail-filters",
    help="ProtonMail Filter Consolidation Tool - safely consolidate your filters into Sieve scripts.",
    add_completion=False,
)
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
):
    """Scrape current filters and save to a timestamped backup."""
    from src.scraper.protonmail_scraper import ProtonMailScraper

    creds = _get_credentials(credentials_file, manual_login)

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds)
        try:
            with console.status("[bold green]Initializing browser..."):
                await scraper.initialize()

            with console.status("[bold green]Logging in..."):
                await scraper.login()

            with console.status("[bold green]Navigating to filters..."):
                await scraper.navigate_to_filters()

            console.print("[bold green]Scraping filters...")
            raw_filters = await scraper.scrape_all_filters()
            console.print(f"[green]Scraped {len(raw_filters)} filters")

            # Parse filters
            filters = parse_scraped_filters(raw_filters)

            # Create backup
            manager = BackupManager()
            bkup = manager.create_backup(filters)

            console.print(Panel(
                f"[bold green]Backup created successfully![/]\n\n"
                f"Filters: {bkup.metadata.filter_count}\n"
                f"Enabled: {bkup.metadata.enabled_count}\n"
                f"Disabled: {bkup.metadata.disabled_count}\n"
                f"Checksum: {bkup.checksum[:30]}...",
                title="Backup Complete",
            ))
        finally:
            await scraper.close()

    asyncio.run(_run())


@app.command("list-backups")
def list_backups():
    """Show all available backups with statistics."""
    manager = BackupManager()
    backups = manager.list_backups()

    if not backups:
        console.print("[yellow]No backups found. Run 'backup' first.")
        return

    table = Table(title="Available Backups")
    table.add_column("Filename", style="cyan")
    table.add_column("Timestamp", style="green")
    table.add_column("Filters", justify="right")
    table.add_column("Enabled", justify="right", style="green")
    table.add_column("Disabled", justify="right", style="yellow")
    table.add_column("Size", justify="right")

    for b in backups:
        size_kb = b["size_bytes"] / 1024
        table.add_row(
            b["filename"],
            b["timestamp"][:19] if b["timestamp"] else "?",
            str(b["filter_count"]),
            str(b["enabled_count"]),
            str(b["disabled_count"]),
            f"{size_kb:.1f} KB",
        )

    console.print(table)


@app.command()
def analyze(
    backup_id: str = typer.Option("latest", "--backup", help="Backup identifier (timestamp or 'latest')"),
):
    """Analyze filter patterns and consolidation opportunities."""
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)

    engine = ConsolidationEngine()
    stats = engine.analyze(bkup.filters)

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
    output_file: str = typer.Option("", "--output", help="Output file for Sieve script"),
):
    """Generate optimized Sieve script from backup (local only, no ProtonMail changes)."""
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)

    engine = ConsolidationEngine()
    consolidated, report = engine.consolidate(bkup.filters)

    generator = SieveGenerator()
    sieve_script = generator.generate(consolidated)

    if output_file:
        out_path = Path(output_file)
    else:
        out_path = OUTPUT_DIR / "consolidated.sieve"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(sieve_script)
    console.print(f"[green]Sieve script saved to: {out_path}")

    console.print(Panel(
        f"[bold]Consolidation Report[/]\n\n"
        f"Original filters: {report.original_count}\n"
        f"Enabled (processed): {report.enabled_count}\n"
        f"Disabled (skipped): {report.disabled_skipped}\n"
        f"Consolidated rules: {report.consolidated_count}\n"
        f"[bold green]Reduction: {report.reduction_percent:.1f}%[/]",
        title="Consolidation Complete",
    ))

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
        bkup = manager.load_backup(backup_id)

        async def _run():
            scraper = ProtonMailScraper(headless=headless, credentials=creds)
            try:
                await scraper.initialize()
                await scraper.login()
                await scraper.navigate_to_filters()
                raw_filters = await scraper.scrape_all_filters()
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
    sieve_file: str = typer.Option(..., "--sieve", help="Path to Sieve script to upload"),
    backup_id: str = typer.Option("latest", "--backup", help="Backup to reference for disabling filters"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    credentials_file: str = typer.Option("", "--credentials-file", help="Credentials file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without applying"),
):
    """Upload Sieve script and disable old UI filters (reversible)."""
    from src.scraper.protonmail_sync import ProtonMailSync

    sieve_path = Path(sieve_file)
    if not sieve_path.exists():
        console.print(f"[red]Sieve file not found: {sieve_path}")
        raise typer.Exit(1)

    sieve_script = sieve_path.read_text()
    creds = _get_credentials(credentials_file, False)
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)

    if dry_run:
        console.print(Panel("[bold yellow]DRY RUN - No changes will be made"))
        console.print(f"\nWould upload Sieve script ({len(sieve_script)} chars)")
        console.print(f"Would disable {bkup.metadata.enabled_count} UI filters")
        return

    async def _run():
        sync_client = ProtonMailSync(headless=headless, credentials=creds)
        try:
            await sync_client.initialize()
            await sync_client.login()
            await sync_client.navigate_to_filters()

            console.print("[bold green]Uploading Sieve script...")
            success = await sync_client.upload_sieve(sieve_script)
            if success:
                console.print("[green]Sieve script uploaded successfully!")
            else:
                console.print("[red]Failed to upload Sieve script")
                return

            console.print("[bold green]Disabling old UI filters...")
            disabled = await sync_client.disable_all_ui_filters()
            console.print(f"[green]Disabled {disabled} filters")

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
):
    """Restore filters to previous backup state."""
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync
    from src.backup.restore_engine import RestoreEngine

    creds = _get_credentials(credentials_file, False)
    manager = BackupManager()
    bkup = manager.load_backup(backup_id)

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            raw_filters = await scraper.scrape_all_filters()
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
):
    """Delete all disabled filters (with confirmation)."""
    from src.scraper.protonmail_scraper import ProtonMailScraper
    from src.scraper.protonmail_sync import ProtonMailSync

    creds = _get_credentials(credentials_file, False)

    async def _run():
        scraper = ProtonMailScraper(headless=headless, credentials=creds)
        try:
            await scraper.initialize()
            await scraper.login()
            await scraper.navigate_to_filters()
            raw_filters = await scraper.scrape_all_filters()
            filters = parse_scraped_filters(raw_filters)
        finally:
            await scraper.close()

        disabled = [f for f in filters if not f.enabled]

        if not disabled:
            console.print("[green]No disabled filters to clean up.")
            return

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

            deleted = 0
            for f in disabled:
                if await sync_client.delete_filter(f.name):
                    deleted += 1
                    console.print(f"  [red]Deleted: {f.name}")

            console.print(f"\n[green]Deleted {deleted}/{len(disabled)} filters")
        finally:
            await sync_client.close()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
