"""Tests for `show-backup` (offline): the raw evidence is actually viewable (D5)."""

import pytest
from typer.testing import CliRunner

import src.utils.config
from src.main import app
from src.backup.backup_manager import BackupManager
from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, ConditionType, Operator, ActionType,
    ScrapeEvidence,
)

runner = CliRunner()


@pytest.fixture
def snapshots_dir(tmp_path, monkeypatch):
    import src.main
    import src.backup.backup_manager
    from rich.console import Console
    d = tmp_path / "snapshots"
    d.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", d)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", d)
    monkeypatch.setattr(src.main, "console", Console(width=200))
    return d


def _filters():
    wizard = ProtonMailFilter(
        name="Work Label",
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="[b]boss@x")],
        actions=[FilterAction(type=ActionType.LABEL, parameters={"label": "Work"})],
        raw=ScrapeEvidence(conditions_text="The sender is exactly boss@x", actions_text="Label as\nWork"),
        scrape_issues=["label row unreadable"],
    )
    sieve = ProtonMailFilter(name="My Script", raw=ScrapeEvidence(sieve_text='require "fileinto";\nkeep;'))
    legacy = ProtonMailFilter(name="Legacy")
    return [wizard, sieve, legacy]


def test_show_raw_prints_evidence_and_issues(snapshots_dir):
    BackupManager(snapshots_dir).create_backup(_filters())
    result = runner.invoke(app, ["show-backup", "--show-raw"])
    assert result.exit_code == 0, result.output
    assert "The sender is exactly boss@x" in result.output
    assert "Label as" in result.output and "Work" in result.output
    assert "label row unreadable" in result.output
    assert 'require "fileinto";' in result.output
    assert "No raw evidence (backed up before format 1.1)" in result.output


def test_without_flag_no_raw_text(snapshots_dir):
    BackupManager(snapshots_dir).create_backup(_filters())
    result = runner.invoke(app, ["show-backup"])
    assert result.exit_code == 0, result.output
    assert "The sender is exactly" not in result.output


def test_user_text_is_not_read_as_markup(snapshots_dir):
    """A condition value like "[b]boss@x" is printed as typed, not swallowed by Rich."""
    BackupManager(snapshots_dir).create_backup(_filters())
    result = runner.invoke(app, ["show-backup"])
    assert "[b]boss@x" in result.output
