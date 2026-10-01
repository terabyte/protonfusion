"""CLI tests for what `consolidate` puts in the script, the archive and the manifest.

Offline: consolidate only reads and writes the snapshot directory.
"""

import json

import pytest
from typer.testing import CliRunner

import src.utils.config
from src.main import app
from src.backup.backup_manager import BackupManager
from src.consolidator.consolidation_engine import ConsolidationEngine
from src.models.filter_models import (
    ProtonMailFilter, FilterStatus, ScrapeEvidence,
)

runner = CliRunner()

EVIDENCE = {"conditions_text": "c", "actions_text": "a", "sieve_text": ""}


def _filter(name, conditions, actions, enabled=True, logic="and") -> ProtonMailFilter:
    """A filter as a backup holds it; unknown values are quarantined by the model."""
    return ProtonMailFilter.model_validate({
        "name": name,
        "enabled": enabled,
        "logic": logic,
        "conditions": conditions,
        "actions": actions,
        "raw": EVIDENCE,
    })


SENDER_A = {"type": "sender", "operator": "is", "value": "a@x.com"}
SENDER_B = {"type": "sender", "operator": "is", "value": "b@x.com"}
UNKNOWN_BODY = {"type": "body", "operator": "contains", "value": "sale"}
DELETE = {"type": "delete", "parameters": {}}
LABEL_WORK = {"type": "label", "parameters": {"label": "Work"}}


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    import src.main
    from rich.console import Console
    monkeypatch.setattr(src.main, "console", Console(width=300))


@pytest.fixture
def snapshots_dir(tmp_path, monkeypatch):
    import src.backup.backup_manager
    d = tmp_path / "snapshots"
    d.mkdir()
    monkeypatch.setattr(src.utils.config, "SNAPSHOTS_DIR", d)
    monkeypatch.setattr(src.backup.backup_manager, "SNAPSHOTS_DIR", d)
    return d


def _script(snapshots_dir) -> str:
    return (snapshots_dir / "latest" / "consolidated.sieve").read_text()


def _manifest(snapshots_dir) -> dict:
    return json.loads((snapshots_dir / "latest" / "manifest.json").read_text())


class TestIncompleteFiltersExcluded:
    """P2: a filter not fully read is left out of the script, not merely warned about."""

    def test_only_condition_quarantined_produces_no_rule(self, snapshots_dir):
        """The panel's repro: its only condition was dropped, so as read it is an
        unconditional delete. It must not reach the script at all."""
        bad = _filter("Delete Sales", [UNKNOWN_BODY], [DELETE])
        assert not bad.is_complete and bad.conditions == []
        BackupManager(snapshots_dir).create_backup([bad, _filter("Work", [SENDER_A], [LABEL_WORK])])

        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        script = _script(snapshots_dir)
        assert "discard" not in script
        assert '"Work"' in script
        assert "Left out of the script: 1" in result.output
        assert "- Delete Sales" in result.output
        assert "unknown condition type 'body'" in result.output

    def test_and_filter_missing_one_condition_excluded(self, snapshots_dir):
        """As read, "a@x AND body contains sale" is just "a@x", which deletes more."""
        bad = _filter("Delete A Sales", [SENDER_A, UNKNOWN_BODY], [DELETE])
        BackupManager(snapshots_dir).create_backup([bad])

        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        script = _script(snapshots_dir)
        assert "a@x.com" not in script
        assert "discard" not in script

    def test_excluded_recorded_in_manifest_and_not_archived(self, snapshots_dir):
        bad = _filter("Delete A Sales", [SENDER_A, UNKNOWN_BODY], [DELETE])
        BackupManager(snapshots_dir).create_backup([bad])
        runner.invoke(app, ["consolidate"])

        manifest = _manifest(snapshots_dir)
        assert bad.content_hash not in manifest["filter_hashes"]
        [entry] = manifest["incomplete_excluded"]
        assert entry["name"] == "Delete A Sales"
        assert entry["content_hash"] == bad.content_hash
        assert any("body" in i for i in entry["scrape_issues"])
        assert manifest["incomplete_included"] == []
        archive = BackupManager(snapshots_dir).load_archive(snapshots_dir / "latest")
        assert archive == []

    def test_incomplete_archived_filter_excluded(self, snapshots_dir):
        """The archive is a source too: an incomplete archived filter stays out."""
        manager = BackupManager(snapshots_dir)
        manager.create_backup([])
        from src.models.backup_models import ArchiveEntry
        archived = _filter("Old Delete", [UNKNOWN_BODY], [DELETE]).model_copy(
            update={"status": FilterStatus.ARCHIVED, "enabled": False},
        )
        manager.write_archive(snapshots_dir / "latest", [ArchiveEntry(filter=archived)])

        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "discard" not in _script(snapshots_dir)
        assert "- Old Delete" in result.output

    def test_allow_incomplete_includes_loudly(self, snapshots_dir):
        bad = _filter("Delete A Sales", [SENDER_A, UNKNOWN_BODY], [DELETE])
        BackupManager(snapshots_dir).create_backup([bad])

        result = runner.invoke(app, ["consolidate", "--allow-incomplete"])
        assert result.exit_code == 0, result.output
        assert "a@x.com" in _script(snapshots_dir)
        assert "WARNING: --allow-incomplete given" in result.output
        assert "- Delete A Sales" in result.output
        manifest = _manifest(snapshots_dir)
        assert manifest["incomplete_excluded"] == []
        assert [e["name"] for e in manifest["incomplete_included"]] == ["Delete A Sales"]


def test_engine_excludes_incomplete_by_default():
    bad = _filter("Bad", [UNKNOWN_BODY], [DELETE])
    good = _filter("Good", [SENDER_A], [LABEL_WORK])
    consolidated, report = ConsolidationEngine().consolidate([bad, good])
    assert [cf.source_filters for cf in consolidated] == [["Good"]]
    assert report.incomplete_excluded == [bad]

    consolidated, report = ConsolidationEngine().consolidate([bad, good], allow_incomplete=True)
    assert sorted(n for cf in consolidated for n in cf.source_filters) == ["Bad", "Good"]
    assert report.incomplete_included == [bad]


class TestTrackedByContentHash:
    """P9: the script, archive and manifest follow content_hash, never the name."""

    @pytest.fixture
    def same_name_snapshot(self, snapshots_dir):
        """An enabled "News" that labels, and a disabled "News" that deletes."""
        enabled = _filter("News", [SENDER_A], [LABEL_WORK])
        disabled = _filter("News", [SENDER_B], [DELETE], enabled=False)
        BackupManager(snapshots_dir).create_backup([enabled, disabled])
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        return enabled, disabled

    def test_disabled_namesake_not_in_script(self, snapshots_dir, same_name_snapshot):
        script = _script(snapshots_dir)
        assert "a@x.com" in script
        assert "b@x.com" not in script and "discard" not in script

    def test_disabled_namesake_not_archived(self, snapshots_dir, same_name_snapshot):
        enabled, disabled = same_name_snapshot
        archive = BackupManager(snapshots_dir).load_archive(snapshots_dir / "latest")
        assert [e.filter.content_hash for e in archive] == [enabled.content_hash]

    def test_disabled_namesake_not_in_manifest(self, snapshots_dir, same_name_snapshot):
        enabled, disabled = same_name_snapshot
        assert _manifest(snapshots_dir)["filter_hashes"] == [enabled.content_hash]

    def test_disabled_rule_stays_out_on_next_consolidate(self, snapshots_dir, same_name_snapshot):
        """The panel's repro: the disabled discard rule came back live next time."""
        manager = BackupManager(snapshots_dir)
        manager.promote_manifest(snapshots_dir / "latest")  # as a successful sync would
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "discard" not in _script(snapshots_dir)

    def test_identical_duplicates_archived_once(self, snapshots_dir):
        twin = _filter("Twin", [SENDER_A], [LABEL_WORK])
        BackupManager(snapshots_dir).create_backup([twin, twin.model_copy()])
        runner.invoke(app, ["consolidate"])
        archive = BackupManager(snapshots_dir).load_archive(snapshots_dir / "latest")
        assert len(archive) == 1
