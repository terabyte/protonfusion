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


def test_manifest_records_absolute_script_path(snapshots_dir, tmp_path, monkeypatch):
    """A relative --output is stored resolved, so sync matches it from any directory."""
    BackupManager(snapshots_dir).create_backup([_filter("Good", [SENDER_A], [LABEL_WORK])])
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["consolidate", "--output", "out/rules.sieve"])
    assert result.exit_code == 0, result.output
    assert _manifest(snapshots_dir)["sieve_file"] == str((tmp_path / "out" / "rules.sieve").resolve())


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


class TestOldSnapshot:
    """D4: backups from before the strict parser may hold misread operators."""

    @staticmethod
    def _set_version(snapshots_dir, version):
        """Rewrite backup.json's format version (the checksum does not cover it
        for formats with these fields, so the file still verifies)."""
        path = snapshots_dir / "latest" / "backup.json"
        data = json.loads(path.read_text())
        data["version"] = version
        path.write_text(json.dumps(data))

    @pytest.fixture
    def old_snapshot(self, snapshots_dir):
        BackupManager(snapshots_dir).create_backup([_filter("Work", [SENDER_A], [LABEL_WORK])])
        self._set_version(snapshots_dir, "1.2")
        return snapshots_dir

    def test_consolidate_warns(self, old_snapshot):
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Old Snapshot" in result.output
        assert '"is not" stored as "is"' in result.output
        assert "Run 'backup' again" in result.output

    def test_warning_does_not_claim_the_backup_was_misread(self, old_snapshot):
        """V1: format 1.2 was also written by strict-parser builds, and the file
        cannot say which, so the panel says "may", never "was"."""
        result = runner.invoke(app, ["consolidate"])
        assert "may have been written by a ProtonFusion that misread" in result.output
        assert "written by an older ProtonFusion that misread" not in result.output

    def test_consolidate_archives_nothing_from_old_snapshot(self, old_snapshot):
        """V1: archive.json is carried into every later snapshot, so a misread
        rule archived now would outlive the fresh backup the warning asks for."""
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        assert "Work" in _script(old_snapshot)
        assert BackupManager(old_snapshot).load_archive(old_snapshot / "latest") == []

    def test_current_snapshot_not_warned(self, snapshots_dir):
        BackupManager(snapshots_dir).create_backup([_filter("Work", [SENDER_A], [LABEL_WORK])])
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Old Snapshot" not in result.output

    @pytest.mark.parametrize("args", [[], ["--dry-run"]])
    def test_sync_refuses(self, old_snapshot, args):
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        result = runner.invoke(app, ["sync", *args])
        assert result.exit_code == 1
        assert "Sync refused" in result.output
        assert "--allow-old-snapshot" in result.output

    def test_sync_override_proceeds(self, old_snapshot):
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        result = runner.invoke(app, ["sync", "--dry-run", "--allow-old-snapshot"])
        assert result.exit_code == 0, result.output
        assert "--allow-old-snapshot given" in result.output
        assert "DRY RUN" in result.output

    # --- V1: archive entries from old backups ---

    MISREAD = _filter("Old rule", [SENDER_B], [DELETE])

    @staticmethod
    def _write_legacy_archive(snapshots_dir, filters, source_format=None):
        """Write archive.json as an older version would: entries with no
        source_format key at all (or, given one, stamped with it)."""
        entries = []
        for f in filters:
            archived = f.model_copy(update={"status": FilterStatus.ARCHIVED, "enabled": False})
            entry = {"filter": archived.model_dump(mode="json"), "archived_at": "", "source_snapshot": "old"}
            if source_format is not None:
                entry["source_format"] = source_format
            entries.append(entry)
        (snapshots_dir / "latest" / "archive.json").write_text(json.dumps({"version": "1.0", "entries": entries}))

    @pytest.fixture
    def old_archive(self, snapshots_dir):
        """A current backup whose archive.json holds an unstamped entry the
        backup does not confirm."""
        BackupManager(snapshots_dir).create_backup([_filter("Work", [SENDER_A], [LABEL_WORK])])
        self._write_legacy_archive(snapshots_dir, [self.MISREAD])
        return snapshots_dir

    def test_unstamped_archive_entry_left_out_with_warning(self, old_archive):
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Old Archive Entries" in result.output
        assert "- Old rule (from snapshot old)" in result.output
        assert "left out of this script" in result.output
        script = _script(old_archive)
        assert "a@x.com" in script and "b@x.com" not in script
        # Kept in the archive (not silently deleted), still unstamped
        entries = BackupManager(old_archive).load_archive(old_archive / "latest")
        old = [e for e in entries if e.filter.name == "Old rule"]
        assert len(old) == 1 and old[0].source_format is None

    def test_pre_1_3_stamped_entry_left_out(self, snapshots_dir):
        BackupManager(snapshots_dir).create_backup([_filter("Work", [SENDER_A], [LABEL_WORK])])
        self._write_legacy_archive(snapshots_dir, [self.MISREAD], source_format="1.2")
        result = runner.invoke(app, ["consolidate"])
        assert "Old Archive Entries" in result.output
        assert "b@x.com" not in _script(snapshots_dir)

    def test_current_stamped_entry_used(self, snapshots_dir):
        from src.models.backup_models import BACKUP_FORMAT_VERSION
        BackupManager(snapshots_dir).create_backup([_filter("Work", [SENDER_A], [LABEL_WORK])])
        self._write_legacy_archive(snapshots_dir, [self.MISREAD], source_format=BACKUP_FORMAT_VERSION)
        result = runner.invoke(app, ["consolidate"])
        assert "Old Archive Entries" not in result.output
        assert "b@x.com" in _script(snapshots_dir)

    def test_old_entry_confirmed_by_current_backup_used(self, snapshots_dir):
        """The fresh strict scrape read the same content, so it was not misread."""
        BackupManager(snapshots_dir).create_backup([
            _filter("Work", [SENDER_A], [LABEL_WORK]), _filter("Old rule", [SENDER_B], [DELETE], enabled=False),
        ])
        self._write_legacy_archive(snapshots_dir, [self.MISREAD])
        result = runner.invoke(app, ["consolidate"])
        assert "Old Archive Entries" not in result.output
        assert "b@x.com" in _script(snapshots_dir)

    def test_consolidate_stamps_new_entries(self, snapshots_dir):
        from src.models.backup_models import BACKUP_FORMAT_VERSION
        BackupManager(snapshots_dir).create_backup([_filter("Work", [SENDER_A], [LABEL_WORK])])
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        entries = BackupManager(snapshots_dir).load_archive(snapshots_dir / "latest")
        assert [e.source_format for e in entries] == [BACKUP_FORMAT_VERSION]

    def test_sync_after_consolidate_left_entry_out_proceeds(self, old_archive):
        assert runner.invoke(app, ["consolidate"]).exit_code == 0
        result = runner.invoke(app, ["sync", "--dry-run"])
        assert result.exit_code == 0, result.output

    @pytest.fixture
    def script_with_old_rule(self, old_archive, tmp_path):
        """A --sieve script (no manifest describes it) holding the old entry's rule."""
        from src.generator.sieve_generator import SieveGenerator
        consolidated, _ = ConsolidationEngine().consolidate(
            [_filter("Work", [SENDER_A], [LABEL_WORK]), self.MISREAD], include_disabled=True,
        )
        path = tmp_path / "s.sieve"
        path.write_text(SieveGenerator.merge_with_existing(SieveGenerator().generate(consolidated), ""))
        return str(path)

    def test_sync_refuses_script_drawing_on_old_entry(self, script_with_old_rule):
        result = runner.invoke(app, ["sync", "--dry-run", "--sieve", script_with_old_rule])
        assert result.exit_code == 1, result.output
        assert "Old Archive Entries" in result.output
        assert "This script holds their rules" in result.output
        assert "--allow-old-snapshot" in result.output

    def test_sync_old_entry_override_proceeds(self, script_with_old_rule):
        result = runner.invoke(app, ["sync", "--dry-run", "--allow-old-snapshot", "--sieve", script_with_old_rule])
        assert result.exit_code == 0, result.output
        assert "--allow-old-snapshot given" in result.output

    def test_format_1_0_is_old(self):
        from src.backup.backup_manager import predates_strict_parser
        from src.models.backup_models import Backup, BACKUP_FORMAT_VERSION
        assert predates_strict_parser(Backup(version="1.0"))
        assert predates_strict_parser(Backup(version="garbage"))
        assert not predates_strict_parser(Backup(version=BACKUP_FORMAT_VERSION))
