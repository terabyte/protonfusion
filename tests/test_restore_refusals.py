"""Restore refusals that a mutation run showed no other test held (no browser)."""

from typer.testing import CliRunner

from src.backup.backup_manager import BackupManager
from src.main import SIEVE_FILTER_NAME, app
from tests.test_restore import NEW_SCRIPT, FakeBrowser, _filter, _sieve, cli_env  # noqa: F401 (fixture)

runner = CliRunner()


def test_empty_script_with_two_protonfusion_filters_refuses(cli_env):
    """--allow-empty-script disables ProtonFusion's filter, but only if there is one (RS9)."""
    BackupManager(cli_env).create_backup([_filter("Old", "old@x", enabled=True)], sieve_script="")
    FakeBrowser.current = [
        _filter("Old", "old@x", enabled=False, priority=0),
        _sieve(SIEVE_FILTER_NAME, NEW_SCRIPT, enabled=True, priority=1),
        _sieve(SIEVE_FILTER_NAME, "keep;", enabled=True, priority=2),
    ]
    FakeBrowser.live_script = NEW_SCRIPT

    result = runner.invoke(app, ["restore", "--backup", "latest", "--allow-empty-script"], input="y\n")
    assert result.exit_code == 1, result.output
    assert f"Found 2 Sieve filters named '{SIEVE_FILTER_NAME}'" in result.output
    assert "Restore refused. Nothing was changed." in result.output
    assert FakeBrowser.calls == []
