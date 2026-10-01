"""CLI tests for the data-loss guards on `backup` and `cleanup`.

The browser classes are replaced with fakes, so these run offline: they
check what the commands do with scraped data, not the scraping itself.
"""

import json

import pytest
from typer.testing import CliRunner

import src.utils.config
import src.scraper.protonmail_scraper
from src.main import app
from src.backup.backup_manager import BackupManager

runner = CliRunner()


def _raw_filter(name, enabled=True, issues=None, raw=True, actions=None):
    """A scraped-filter dict as ProtonMailScraper returns it."""
    return {
        "name": name,
        "enabled": enabled,
        "priority": 0,
        "logic": "and",
        "conditions": [{"type": "sender", "operator": "contains", "value": f"{name}@example.com"}],
        "actions": actions if actions is not None else [{"type": "label", "parameters": {"label": "Work"}}],
        "raw": {"conditions_text": "the sender", "actions_text": "Label as Work", "sieve_text": ""} if raw else None,
        "scrape_issues": issues or [],
    }


class FakeScraper:
    """Stands in for ProtonMailScraper; returns FakeScraper.raw_filters."""
    raw_filters: list = []

    def __init__(self, *args, **kwargs):
        self.account_email = "test@proton.me"

    async def initialize(self):
        pass

    async def login(self):
        pass

    async def navigate_to_filters(self):
        pass

    async def scrape_all_filters(self, workers=1):
        return FakeScraper.raw_filters

    async def read_sieve_script(self, filter_name=""):
        return ""

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """Keep Rich from wrapping long names/issues in CliRunner output."""
    import src.main
    from rich.console import Console
    monkeypatch.setattr(src.main, "console", Console(width=200))


@pytest.fixture
def cli_snapshots_dir(tmp_path, monkeypatch):
    """Temp snapshots dir used by the CLI's BackupManager()."""
    import src.backup.backup_manager
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", snapshots_dir)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", snapshots_dir)
    return snapshots_dir


@pytest.fixture
def fake_scraper(monkeypatch):
    monkeypatch.setattr(src.scraper.protonmail_scraper, "ProtonMailScraper", FakeScraper)
    FakeScraper.raw_filters = []
    return FakeScraper


class TestBackupGuard:
    """`backup` must fail loudly when a filter was not fully read."""

    def test_complete_scrape_saves(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [_raw_filter("Good")]
        result = runner.invoke(app, ["backup", "--headless"])
        assert result.exit_code == 0, result.output
        data = json.loads((cli_snapshots_dir / "latest" / "backup.json").read_text())
        assert data["filters"][0]["actions"][0]["parameters"]["label"] == "Work"
        assert data["filters"][0]["raw"]["actions_text"] == "Label as Work"

    def test_incomplete_scrape_fails_and_names_filter(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [
            _raw_filter("Good"),
            _raw_filter("Has Autoreply", issues=["unsupported action row 'filter-modal:autoreply-row'"]),
        ]
        result = runner.invoke(app, ["backup", "--headless"])
        assert result.exit_code == 1
        assert "Has Autoreply" in result.output
        assert "filter-modal:autoreply-row" in result.output
        assert "Backup NOT saved" in result.output
        assert not (cli_snapshots_dir / "latest").exists()

    def test_allow_incomplete_saves_flagged(self, cli_snapshots_dir, fake_scraper):
        fake_scraper.raw_filters = [_raw_filter("Odd", issues=["label row unreadable"])]
        result = runner.invoke(app, ["backup", "--headless", "--allow-incomplete"])
        assert result.exit_code == 0, result.output
        data = json.loads((cli_snapshots_dir / "latest" / "backup.json").read_text())
        assert data["filters"][0]["scrape_issues"] == ["label row unreadable"]

