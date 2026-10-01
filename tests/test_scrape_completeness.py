"""Integration tests for the scraper's completeness guard.

Scrapes the mock page's edge-case set (?set=edge), whose filters each use
a field or layout the scraper cannot fully read. Each must come back with
scrape_issues naming the problem, never silently reduced to the parts the
scraper did understand.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

from src.scraper.protonmail_scraper import ProtonMailScraper

MOCK_HTML = Path(__file__).parent / "fixtures" / "mock_filters_page.html"
EDGE_URL = f"file://{MOCK_HTML.resolve()}?set=edge"


@pytest.fixture(scope="module")
def edge_filters():
    """Scrape the edge-case set once for all tests in this module."""
    import asyncio

    async def _scrape():
        scraper = ProtonMailScraper(headless=True)
        try:
            await scraper.initialize()
            await scraper.page.goto(EDGE_URL, wait_until="domcontentloaded")
            await scraper.page.wait_for_timeout(500)
            return await scraper.scrape_all_filters(workers=1)
        finally:
            await scraper.close()

    filters = asyncio.run(_scrape())
    return {f["name"]: f for f in filters}


def test_clean_filter_not_flagged(edge_filters):
    f = edge_filters["Clean Labelled"]
    assert f["scrape_issues"] == []
    assert {"type": "label", "parameters": {"label": "Work"}} in f["actions"]


def test_auto_reply_on_flagged(edge_filters):
    issues = edge_filters["Has Autoreply"]["scrape_issues"]
    assert "auto-reply action not supported" in issues


def test_auto_reply_off_not_flagged(edge_filters):
    """The auto-reply row is on every filter; off is the normal case."""
    assert edge_filters["Clean Labelled"]["scrape_issues"] == []


def test_unknown_action_row_flagged(edge_filters):
    f = edge_filters["Unknown Row"]
    assert any("filter-modal:forward-row" in i for i in f["scrape_issues"])
    # The raw evidence still records what the unknown row showed.
    assert "Forward to" in f["raw"]["actions_text"]


def test_unnamed_label_option_flagged(edge_filters):
    issues = edge_filters["Unnamed Label Option"]["scrape_issues"]
    assert any("no readable name" in i for i in issues)


def test_unexplained_label_text_flagged(edge_filters):
    issues = edge_filters["Unexplained Label Text"]["scrape_issues"]
    assert any("Applied: Receipts" in i for i in issues)


def test_unknown_operator_flagged(edge_filters):
    issues = edge_filters["Unknown Operator"]["scrape_issues"]
    assert any("does not contain" in i for i in issues)


def test_unknown_mark_as_option_flagged(edge_filters):
    f = edge_filters["Extra Mark Option"]
    assert any("Pinned" in i for i in f["scrape_issues"])
    assert {"type": "mark_read", "parameters": {}} in f["actions"]


def test_sieve_filter_keeps_script(edge_filters):
    f = edge_filters["My Sieve"]
    assert f["scrape_issues"] == []
    assert 'fileinto "X"' in f["raw"]["sieve_text"]
    assert f["is_sieve"] is True


def test_wizard_filter_not_marked_sieve(edge_filters):
    assert edge_filters["Clean Labelled"]["is_sieve"] is False


def test_backup_refuses_auto_reply_filter(edge_filters, tmp_path, monkeypatch):
    """End to end from the real scrape: an auto-reply filter makes backup refuse.

    The scraper flagging it (test_auto_reply_on_flagged) is only half the
    guard; this pins that the flag reaches backup and stops the save.
    """
    import src.utils.config
    import src.backup.backup_manager
    import src.scraper.protonmail_scraper
    from typer.testing import CliRunner
    from src.main import app

    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", snapshots_dir)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", snapshots_dir)

    class ScrapedEdgeSet:
        """Returns the real scrape of the edge set; no browser needed again."""
        account_email = "test@proton.me"

        def __init__(self, *args, **kwargs):
            pass

        async def initialize(self):
            pass

        async def login(self):
            pass

        async def navigate_to_filters(self):
            pass

        async def scrape_all_filters(self, workers=1):
            return [edge_filters["Clean Labelled"], edge_filters["Has Autoreply"]]

        async def read_sieve_script(self, filter_name=""):
            return ""

        async def close(self):
            pass

    monkeypatch.setattr(src.scraper.protonmail_scraper, "ProtonMailScraper", ScrapedEdgeSet)
    result = CliRunner().invoke(app, ["backup", "--headless"])
    assert result.exit_code == 1, result.output
    assert "Has Autoreply" in result.output
    assert "auto-reply action not supported" in result.output
    assert "Backup NOT saved" in result.output
    assert not (snapshots_dir / "latest").exists()
