"""Backups written by older ProtonFusion versions still verify and still restore (V3).

The fixtures are built inline in the exact layout the pre-fix build wrote:
backup.json holds each filter as that build serialized it, and the checksum
is SHA-256 over those dicts plus the Sieve script, as its compute_checksum
did. Nothing here runs the old code.
"""

import hashlib
import json

import pytest

from src.backup.backup_manager import BackupManager, BackupIntegrityError
from src.backup.restore_engine import RestoreEngine
from src.models.filter_models import ProtonMailFilter

EVIDENCE = {"conditions_text": "c", "actions_text": "a", "sieve_text": ""}


def _old_filter(name, conditions, actions, enabled=True, priority=0, fields_from_1_1=True) -> dict:
    """A filter dict as the pre-fix build wrote it (format 1.2, or 1.0 without
    raw, scrape_issues and is_sieve)."""
    data = {
        "name": name, "enabled": enabled, "status": "enabled" if enabled else "disabled",
        "priority": priority, "logic": "and", "conditions": conditions, "actions": actions,
    }
    if fields_from_1_1:
        data.update({"raw": dict(EVIDENCE), "scrape_issues": [], "is_sieve": False})
    return data


def _write_old_backup(snapshots_dir, filters, version="1.2", sieve_script="") -> None:
    """Write backup.json as the old build did, checksummed over the stored dicts."""
    checksum_json = json.dumps({"filters": filters, "sieve_script": sieve_script}, sort_keys=True, default=str)
    data = {
        "version": version, "timestamp": "2026-09-01 00:00:00", "metadata": {"filter_count": len(filters)},
        "filters": filters, "sieve_script": sieve_script,
        "checksum": "sha256:" + hashlib.sha256(checksum_json.encode()).hexdigest(),
    }
    snapshot = snapshots_dir / "2026-09-01_00-00-00"
    snapshot.mkdir()
    (snapshot / "backup.json").write_text(json.dumps(data, indent=2))
    (snapshots_dir / "latest").symlink_to(snapshot.name)


SENDER = {"type": "sender", "operator": "is", "value": "promo@x.com"}
NEWS = {"type": "move_to", "parameters": {"folder": "News"}}

OLD_FILTERS = {
    "trash": _old_filter("Trash promos", [SENDER], [{"type": "delete", "parameters": {}}]),
    "spam": _old_filter("Spam it", [SENDER], [{"type": "move_to", "parameters": {"folder": "Spam"}}]),
    "inbox": _old_filter("Keep", [SENDER], [{"type": "move_to", "parameters": {"folder": "Inbox - Default"}}]),
    "empty_value": _old_filter("Half read", [SENDER, {"type": "subject", "operator": "contains", "value": ""}], [NEWS]),
    "chips": _old_filter("Two senders", [{"type": "sender", "operator": "is", "value": "a@x.com, b@x.com"}], [NEWS]),
    "slash": _old_filter("Slash", [SENDER], [{"type": "move_to", "parameters": {"folder": "Work/a/b"}}]),
}


class TestOldBackupChecksum:
    """The checksum is verified against the JSON as written, before migration."""

    @pytest.mark.parametrize("kind", sorted(OLD_FILTERS))
    def test_unmodified_old_backup_verifies(self, tmp_path, kind):
        _write_old_backup(tmp_path, [OLD_FILTERS[kind]])
        backup = BackupManager(tmp_path).load_backup("latest", ignore_checksum=False)
        assert len(backup.filters) == 1

    def test_migration_still_applied_after_verifying(self, tmp_path):
        _write_old_backup(tmp_path, [OLD_FILTERS["trash"], OLD_FILTERS["spam"]])
        backup = BackupManager(tmp_path).load_backup("latest", ignore_checksum=False)
        assert backup.filters[0].actions[0].type.value == "trash"
        assert backup.filters[1].actions[0].parameters == {"folder": "spam"}

    def test_format_1_0_backup_verifies(self, tmp_path):
        old = _old_filter("Trash promos", [SENDER], [{"type": "delete", "parameters": {}}], fields_from_1_1=False)
        _write_old_backup(tmp_path, [old], version="1.0")
        backup = BackupManager(tmp_path).load_backup("latest", ignore_checksum=False)
        assert backup.filters[0].raw is None

    def test_edited_old_backup_still_refused(self, tmp_path):
        _write_old_backup(tmp_path, [OLD_FILTERS["trash"]])
        path = tmp_path / "latest" / "backup.json"
        data = json.loads(path.read_text())
        data["filters"][0]["conditions"][0]["value"] = "other@x.com"
        path.write_text(json.dumps(data))
        with pytest.raises(BackupIntegrityError):
            BackupManager(tmp_path).load_backup("latest", ignore_checksum=False)

    def test_current_backup_verifies(self, tmp_path):
        live = ProtonMailFilter.model_validate(dict(OLD_FILTERS["chips"], conditions=[
            {"type": "sender", "operator": "is", "values": ["a@x.com", "b@x.com"]},
        ]))
        BackupManager(tmp_path).create_backup([live])
        assert BackupManager(tmp_path).load_backup("latest", ignore_checksum=False).filters[0].content_hash == live.content_hash


def _live(old: dict, **changes) -> ProtonMailFilter:
    """The current scrape of the same filter, disabled since the backup."""
    return ProtonMailFilter.model_validate(dict(old, enabled=False, status="disabled", **changes))


class TestOldBackupRestoreMatching:
    """An old backup's filter matches the live scrape of the unchanged filter."""

    def _plan(self, tmp_path, old, live):
        _write_old_backup(tmp_path, [old])
        backup = BackupManager(tmp_path).load_backup("latest", ignore_checksum=False)
        return RestoreEngine.plan(backup, [live])

    def test_multi_chip_filter_restored(self, tmp_path):
        old = OLD_FILTERS["chips"]
        live = _live(old, conditions=[{"type": "sender", "operator": "is", "values": ["a@x.com", "b@x.com"]}])
        plan = self._plan(tmp_path, old, live)
        assert [b.name for b, _ in plan.to_enable] == ["Two senders"]
        assert plan.not_found == []

    def test_slash_in_folder_name_restored(self, tmp_path):
        """The old scraper stored folder "a/b" under Work unescaped; now it is Work/a\\/b."""
        old = OLD_FILTERS["slash"]
        live = _live(old, actions=[{"type": "move_to", "parameters": {"folder": "Work/a\\/b"}}])
        plan = self._plan(tmp_path, old, live)
        assert [b.name for b, _ in plan.to_enable] == ["Slash"]

    def test_trash_filter_restored(self, tmp_path):
        old = OLD_FILTERS["trash"]
        live = _live(old, actions=[{"type": "trash", "parameters": {}}])
        plan = self._plan(tmp_path, old, live)
        assert [b.name for b, _ in plan.to_enable] == ["Trash promos"]

    def test_changed_filter_still_not_found(self, tmp_path):
        old = OLD_FILTERS["chips"]
        live = _live(old, conditions=[{"type": "sender", "operator": "is", "values": ["a@x.com", "c@x.com"]}])
        plan = self._plan(tmp_path, old, live)
        assert plan.to_enable == [] and len(plan.not_found) == 1

    def test_literal_and_chips_both_live_is_ambiguous(self, tmp_path):
        """legacy_identity cannot tell a ", " literal from two chips, so with both
        in the account nothing is toggled."""
        from src.models.filter_models import legacy_identity
        old = OLD_FILTERS["chips"]
        chips = _live(old, conditions=[{"type": "sender", "operator": "is", "values": ["a@x.com", "b@x.com"]}])
        literal = _live(old, priority=1)
        assert legacy_identity(chips) == legacy_identity(literal)
        _write_old_backup(tmp_path, [old])
        backup = BackupManager(tmp_path).load_backup("latest", ignore_checksum=False)
        plan = RestoreEngine.plan(backup, [chips, literal])
        assert plan.to_enable == [] and len(plan.ambiguous) == 1
