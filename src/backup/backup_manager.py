"""Manage filter backups: create, load, list, delete."""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.models.backup_models import (
    Backup, BackupMetadata, Archive, ArchiveEntry, BACKUP_FORMAT_VERSION,
    STRICT_PARSER_FORMAT_VERSION,
)
from src.consolidator.carry_forward import filter_facts, is_carried
from src.generator.sieve_rules import Fact, SieveParseError, current_forms, extract_section, script_facts
from src.models.filter_models import ProtonMailFilter
from src.utils.config import SNAPSHOTS_DIR, TOOL_VERSION
from src.utils.private_files import write_private_file

logger = logging.getLogger(__name__)

# Filter fields added after backup format 1.0, keyed by the last format
# that did NOT have them. An older backup's checksum was computed before
# they existed, so they are left out when verifying one; otherwise their
# defaults would change the hashed JSON and every old backup would fail
# verification.
EVIDENCE_FIELDS = {"raw", "scrape_issues"}
FIELDS_ADDED_AFTER = {
    "1.0": EVIDENCE_FIELDS | {"is_sieve"},
    "1.1": {"is_sieve"},
}


def _version_tuple(version: str) -> Tuple[int, ...]:
    """"1.2" -> (1, 2). Anything unparsable reads as (0,), i.e. oldest."""
    try:
        return tuple(int(part) for part in version.split("."))
    except (AttributeError, ValueError):
        return (0,)


def format_predates_strict_parser(version: Optional[str]) -> bool:
    """True if data in backup format `version` may come from the old, misreading parser.

    None (format not recorded) counts as old: nothing shows it is not.
    """
    if version is None:
        return True
    return _version_tuple(version) < _version_tuple(STRICT_PARSER_FORMAT_VERSION)


def predates_strict_parser(backup: Backup) -> bool:
    """True if the backup was written before the parser matched values exactly.

    Such a backup may hold conditions whose operator was misread (see
    STRICT_PARSER_FORMAT_VERSION), so building a script from it can widen
    or invert rules.
    """
    return format_predates_strict_parser(backup.version)


# Why an archive entry is unverified (classify_old_entries)
OLD_BACKUP_ENTRY = "old backup"
CARRIED_COPY_ENTRY = "carried copy"
UNSTAMPED_CARRIED_ENTRY = "unstamped carried"


def live_section_facts(backup: Backup) -> Set[Fact]:
    """The facts of the live ProtonFusion section captured in `backup`, in current forms.

    Each live fact is expanded to every form the current generator may
    write for the same rule (current_forms), so a filter's generated facts
    can be tested against it by plain subset. Empty when the backup holds
    no section or one that cannot be parsed: nothing is confirmed then.
    """
    script = backup.sieve_script or ""
    try:
        if extract_section(script) is None:
            return set()
        live = script_facts(script)
    except SieveParseError:
        return set()
    forms: Set[Fact] = set()
    for fact in live:
        forms |= current_forms(fact)
    return forms


def strict_confirmations(backup: Backup) -> Set[str]:
    """Content hashes a fresh, strict read in `backup` confirms.

    Empty for a backup older than the strict parser. Only fully read
    filters with raw evidence count: an incomplete filter's hash covers
    only what was read, so it can equal a misread entry's while the real
    filter differs.
    """
    if predates_strict_parser(backup):
        return set()
    return {f.content_hash for f in backup.filters if f.is_complete and f.raw is not None}


def classify_old_entries(
    entries: List[ArchiveEntry], backup: Backup,
) -> List[Tuple[ArchiveEntry, str]]:
    """The archive entries that may hold a misread rule, each with why, in archive order.

    OLD_BACKUP_ENTRY: archived from a backup older than the strict parser
    (source_format older than 1.3, or unrecorded) and not confirmed. It is
    confirmed when `backup` holds a fully read filter with the same
    content_hash (strict_confirmations): the fresh, strict scrape read
    exactly the same rule, so it was not misread. archive.json is carried
    from snapshot to snapshot, so without this check a misread filter
    archived from an old backup would outlive the re-backup that the
    old-snapshot warning asks for.

    UNSTAMPED_CARRIED_ENTRY: a carried-forward filter (rebuilt from the
    live ProtonFusion section, never scraped) written by an earlier build
    without a source_format. No backup can confirm it, since carried
    filters are not UI filters; instead it is confirmed when every rule it
    generates is in the live section captured in `backup`, in the form
    the current version writes (live_section_facts). It was copied from
    that section, so finding it there unchanged means it still says what
    is running. Its "|"-joined legacy value was split on load (see
    ArchiveEntry), so a literal "|" key in the live section never confirms
    the split reading.

    CARRIED_COPY_ENTRY: a carried-forward filter (rebuilt from the live
    ProtonFusion section) that copies the rule of an unconfirmed old
    entry. The live section was generated from that entry's filter by
    the old version, so it holds the same possibly misread rule, and
    copying it from there confirms nothing. Such a copy stays unverified
    while it shares a rule with an unconfirmed old entry, or while any
    entry recorded in its matches_unverified (the old entries it matched
    when carried, see ArchiveEntry) is unconfirmed: removing the old
    entry does not make the copy any less suspect.
    """
    confirmed = strict_confirmations(backup)
    classified: Dict[int, str] = {}
    suspect_facts: Set = set()
    for e in entries:
        if is_carried(e.filter) or not format_predates_strict_parser(e.source_format):
            continue
        if e.filter.content_hash in confirmed:
            continue
        classified[id(e)] = OLD_BACKUP_ENTRY
        suspect_facts |= filter_facts(e.filter)
    # A hash recorded by a carried copy is cleared once an entry with that
    # hash is confirmed: by the current backup, or stamped by a strict read.
    cleared = confirmed | {
        e.filter.content_hash for e in entries
        if not is_carried(e.filter) and not format_predates_strict_parser(e.source_format)
    }
    live: Optional[Set[Fact]] = None
    for e in entries:
        if not is_carried(e.filter):
            continue
        if any(h not in cleared for h in e.matches_unverified) or filter_facts(e.filter) & suspect_facts:
            classified[id(e)] = CARRIED_COPY_ENTRY
        elif format_predates_strict_parser(e.source_format):
            if live is None:
                live = live_section_facts(backup)
            facts = filter_facts(e.filter)
            if not (facts and facts <= live):
                classified[id(e)] = UNSTAMPED_CARRIED_ENTRY
    return [(e, classified[id(e)]) for e in entries if id(e) in classified]


def unverified_old_entries(entries: List[ArchiveEntry], backup: Backup) -> List[ArchiveEntry]:
    """The archive entries that may hold a misread rule, so must not feed a script.

    See classify_old_entries for which entries these are and why.
    """
    return [e for e, _ in classify_old_entries(entries, backup)]


def suspect_matches(f: ProtonMailFilter, classified: List[Tuple[ArchiveEntry, str]]) -> List[str]:
    """Content hashes of the unconfirmed old entries whose rule `f` may copy.

    `classified` is classify_old_entries' result for the archive. Those
    are the unconfirmed old entries sharing a rule with `f`, plus what any
    unverified carried copy sharing a rule with `f` recorded (so a copy of
    a copy inherits its suspicion, even after the old entry it matched was
    removed). For a filter carry-forward is about to rebuild from the live
    section, a non-empty result means the live rule it copies may be a
    misread rule, so the copy must not be trusted (recorded in
    ArchiveEntry.matches_unverified). Also used to record the matches of a
    copy found by its rule alone, so its suspicion outlives the old entry.
    """
    facts = filter_facts(f)
    hashes: Set[str] = set()
    for e, kind in classified:
        if e.filter is f or not filter_facts(e.filter) & facts:
            continue
        if kind == OLD_BACKUP_ENTRY and not is_carried(e.filter):
            hashes.add(e.filter.content_hash)
        elif kind == CARRIED_COPY_ENTRY:
            hashes |= set(e.matches_unverified)
    return sorted(hashes)


def _checksum_of(filter_data: list, sieve_script: str) -> str:
    """SHA-256 over already-serialized filters and the Sieve script."""
    checksum_data = {"filters": filter_data, "sieve_script": sieve_script}
    checksum_json = json.dumps(checksum_data, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(checksum_json.encode()).hexdigest()


def compute_checksum(filters: List[ProtonMailFilter], sieve_script: str, version: str) -> str:
    """SHA-256 over the filters and Sieve script, in the layout of `version`.

    What `create_backup` stores. Verifying a stored backup uses
    compute_stored_checksum instead, over the JSON as written.
    """
    exclude = FIELDS_ADDED_AFTER.get(version)
    return _checksum_of([f.model_dump(exclude=exclude) for f in filters], sieve_script)


def compute_stored_checksum(data: dict) -> str:
    """The checksum of a backup.json as loaded, before any model migration.

    Reading a filter into the model rewrites what older versions wrote
    (action "delete" becomes "trash", folder "Spam" becomes "spam", an
    empty-value condition is quarantined), so a checksum over the model
    would refuse every unmodified old backup holding such a filter. The
    JSON as written is exactly what the checksum covered, minus the fields
    its format lacked (FIELDS_ADDED_AFTER).
    """
    version = data.get("version", Backup.model_fields["version"].default)
    exclude = FIELDS_ADDED_AFTER.get(version) or set()
    filter_data = [
        {key: value for key, value in f.items() if key not in exclude} if isinstance(f, dict) else f
        for f in data.get("filters", [])
    ]
    return _checksum_of(filter_data, data.get("sieve_script", ""))


class BackupIntegrityError(Exception):
    """backup.json does not match its checksum (or has none).

    Raised by load_backup so no command builds a script from, restores
    from, or verifies a deletion against a backup that changed after
    'backup' wrote it. The message says how to proceed.
    """


def unverified_for_deletion(
    live_filters: List[ProtonMailFilter], backed_up: List[ProtonMailFilter],
) -> List[Tuple[ProtonMailFilter, str]]:
    """Return the live filters that are not safe to delete, each with a reason.

    Deleting a UI filter is only safe if a backup holds a copy that is
    known to be whole: same content_hash as the live filter (so the backup
    is of this exact filter), no scrape issues, and raw evidence to recover
    from if the parser still missed something. The live scrape itself must
    also be complete, or the hash match proves nothing.

    `backed_up` is every copy available to check against, typically the
    latest snapshot's backup.json filters plus its archive.json entries.
    """
    copies_by_hash: Dict[str, List[ProtonMailFilter]] = {}
    names_backed_up = set()
    for f in backed_up:
        copies_by_hash.setdefault(f.content_hash, []).append(f)
        names_backed_up.add(f.name)

    unverified = []
    for live in live_filters:
        if not live.is_complete:
            unverified.append((live, "live filter could not be fully read: " + "; ".join(live.scrape_issues)))
            continue
        copies = copies_by_hash.get(live.content_hash, [])
        if any(c.is_complete and c.raw is not None for c in copies):
            continue
        if not copies:
            if live.name in names_backed_up:
                reason = "backup copy differs from the live filter (run 'backup' again)"
            else:
                reason = "no backup copy of this filter (run 'backup' first)"
        elif all(c.raw is None for c in copies):
            reason = "backup copy has no raw evidence (made before backup format 1.1); run 'backup' again"
        else:
            issues = sorted({i for c in copies for i in c.scrape_issues})
            reason = "backup copy is incomplete: " + "; ".join(issues)
        unverified.append((live, reason))
    return unverified


class BackupManager:
    """Manages filter backups inside timestamped snapshot directories."""

    # When True, load_backup loads a backup whose checksum does not match
    # instead of raising. Set for a whole CLI run by the global
    # --ignore-checksum option; the escape hatch for a deliberately
    # hand-edited backup.
    ignore_checksum: bool = False

    def __init__(self, snapshots_dir: Optional[Path] = None):
        self.snapshots_dir = snapshots_dir or SNAPSHOTS_DIR
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        # Directory of the snapshot create_backup most recently wrote
        self.last_snapshot_dir: Optional[Path] = None

    def create_backup(
        self, filters: List[ProtonMailFilter], account_email: str = "", sieve_script: str = "",
        make_latest: bool = True,
    ) -> Backup:
        """Create a new backup inside a timestamped snapshot directory.

        The directory name is the timestamp, with a -2, -3, ... suffix if a
        snapshot from the same second exists, so one never overwrites
        another. Its path is left in self.last_snapshot_dir. With
        make_latest=False (restore's safety backup) the `latest` link is
        left where it was.
        """
        now = datetime.now()

        enabled_count = sum(1 for f in filters if f.enabled)
        disabled_count = len(filters) - enabled_count

        metadata = BackupMetadata(
            filter_count=len(filters),
            enabled_count=enabled_count,
            disabled_count=disabled_count,
            account_email=account_email,
            tool_version=TOOL_VERSION,
        )

        backup = Backup(
            version=BACKUP_FORMAT_VERSION,
            timestamp=now,
            metadata=metadata,
            filters=filters,
            sieve_script=sieve_script,
        )

        # Calculate checksum (includes sieve_script for integrity)
        backup.checksum = compute_checksum(filters, sieve_script, backup.version)

        # Save backup.json inside a new snapshot subdirectory. It holds the
        # user's filter data, so like the session file it is owner-only (a
        # snapshot dir this creates is 0700).
        base = now.strftime("%Y-%m-%d_%H-%M-%S")
        dirname = base
        suffix = 2
        while (self.snapshots_dir / dirname).exists():
            dirname = f"{base}-{suffix}"
            suffix += 1
        snapshot_dir = self.snapshots_dir / dirname
        filepath = snapshot_dir / "backup.json"
        write_private_file(filepath, json.dumps(backup.model_dump(), indent=2, default=str))

        # Carry forward archive from previous snapshot before updating symlink
        self.carry_forward_archive(snapshot_dir)

        # Update latest symlink at snapshots/latest -> dirname
        if make_latest:
            latest_link = self.snapshots_dir / "latest"
            if latest_link.exists() or latest_link.is_symlink():
                latest_link.unlink()
            latest_link.symlink_to(dirname)
        self.last_snapshot_dir = snapshot_dir

        logger.info("Backup created: %s (%d filters)", snapshot_dir, len(filters))
        return backup

    def snapshot_dir_for(self, identifier: str = "latest") -> Path:
        """Resolve a snapshot identifier to its directory path."""
        if identifier == "latest":
            latest_link = self.snapshots_dir / "latest"
            if not latest_link.exists():
                raise FileNotFoundError("No latest snapshot found. Run 'backup' first.")
            return latest_link.resolve()
        # Try as a timestamp dirname
        candidate = self.snapshots_dir / identifier
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"Snapshot not found: {identifier}")

    def load_backup(self, identifier: str = "latest", ignore_checksum: Optional[bool] = None) -> Backup:
        """Load a backup by timestamp or 'latest', verifying its checksum.

        Raises BackupIntegrityError if backup.json has no checksum or does
        not match it, unless ignore_checksum (default: the class-wide
        BackupManager.ignore_checksum) is set, in which case it only warns.
        A hand-edited backup fails this too, by design: the edit may have
        changed what the filters do.
        """
        snapshot_dir = self.snapshot_dir_for(identifier)
        filepath = snapshot_dir / "backup.json"
        if not filepath.exists():
            raise FileNotFoundError(f"No backup.json in snapshot: {snapshot_dir}")

        with open(filepath, "r") as f:
            data = json.load(f)

        # Verified against the JSON as written, before the model migrates
        # anything (see compute_stored_checksum)
        stored_ok = self.verify_stored(data)
        backup = Backup.model_validate(data)
        if not stored_ok:
            if ignore_checksum is None:
                ignore_checksum = self.ignore_checksum
            problem = "has no checksum" if not backup.checksum else "does not match its checksum"
            if not ignore_checksum:
                raise BackupIntegrityError(
                    f"{filepath} {problem}: it was changed after 'backup' wrote it, "
                    "by a hand edit or by corruption. Refusing to use it, since a changed "
                    "backup could drop or alter rules. Run 'backup' again for a fresh snapshot, "
                    "or, if you edited it on purpose, re-run with the global option before the "
                    "command name: 'python -m src.main --ignore-checksum <command> ...'. "
                    "Filters holding values ProtonFusion does not know then load flagged incomplete."
                )
            logger.warning("%s %s; loading it anyway (--ignore-checksum)", filepath, problem)
        logger.info("Loaded backup: %s (%d filters)", filepath, len(backup.filters))
        return backup

    def list_backups(self) -> List[dict]:
        """List all available snapshots with metadata."""
        backups = []

        for entry in sorted(self.snapshots_dir.iterdir()):
            if entry.name == "latest" or not entry.is_dir():
                continue
            backup_file = entry / "backup.json"
            if not backup_file.exists():
                continue
            try:
                with open(backup_file, "r") as f:
                    data = json.load(f)
                backups.append({
                    "snapshot": entry.name,
                    "path": str(entry),
                    "timestamp": data.get("timestamp", ""),
                    "filter_count": data.get("metadata", {}).get("filter_count", 0),
                    "enabled_count": data.get("metadata", {}).get("enabled_count", 0),
                    "disabled_count": data.get("metadata", {}).get("disabled_count", 0),
                    "size_bytes": backup_file.stat().st_size,
                })
            except Exception as e:
                logger.warning("Failed to read snapshot %s: %s", entry, e)

        return backups

    def verify_stored(self, data: dict) -> bool:
        """Verify a backup.json's checksum against its JSON as loaded (what load_backup uses)."""
        checksum = data.get("checksum") if isinstance(data, dict) else None
        if not checksum:
            logger.warning("Backup has no checksum")
            return False
        computed = compute_stored_checksum(data)
        if computed != checksum:
            logger.error("Checksum mismatch! Expected %s, got %s", checksum, computed)
            return False
        return True

    def verify_backup(self, backup: Backup) -> bool:
        """Verify an in-memory backup's checksum against its model.

        For a backup this version wrote. One loaded from an older format
        may have been migrated on load; load_backup verifies the stored
        JSON instead (verify_stored).
        """
        if not backup.checksum:
            logger.warning("Backup has no checksum")
            return False

        computed = compute_checksum(backup.filters, backup.sieve_script, backup.version)

        is_valid = computed == backup.checksum
        if not is_valid:
            logger.error("Checksum mismatch! Expected %s, got %s", backup.checksum, computed)
        return is_valid

    def delete_backup(self, identifier: str) -> bool:
        """Delete a snapshot directory."""
        import shutil
        candidate = self.snapshots_dir / identifier
        if candidate.is_dir():
            shutil.rmtree(candidate)
            logger.info("Deleted snapshot: %s", candidate)
            return True
        logger.warning("Snapshot not found: %s", identifier)
        return False

    # --- Archive methods ---

    def write_archive(self, snapshot_dir: Path, entries: List[ArchiveEntry]):
        """Serialize archive entries to archive.json in the snapshot directory."""
        archive = Archive(entries=entries)
        archive_path = snapshot_dir / "archive.json"
        write_private_file(archive_path, json.dumps(archive.model_dump(), indent=2, default=str))
        logger.info("Archive written: %s (%d entries)", archive_path, len(entries))

    def load_archive(self, snapshot_dir: Path) -> List[ArchiveEntry]:
        """Load archive.json from a snapshot directory. Returns empty list if absent."""
        archive_path = snapshot_dir / "archive.json"
        if not archive_path.exists():
            return []
        data = json.loads(archive_path.read_text())
        archive = Archive.model_validate(data)
        return archive.entries

    def carry_forward_archive(self, target_dir: Path) -> List[ArchiveEntry]:
        """Copy archive.json from the latest symlink to target_dir.

        Returns the carried-forward entries (empty list if no previous archive).
        """
        latest_link = self.snapshots_dir / "latest"
        if not latest_link.exists() and not latest_link.is_symlink():
            return []
        try:
            prev_dir = latest_link.resolve()
        except OSError:
            return []
        if prev_dir == target_dir:
            # Don't carry forward from ourselves
            return []
        entries = self.load_archive(prev_dir)
        if entries:
            self.write_archive(target_dir, entries)
            logger.info("Archive carried forward: %d entries from %s", len(entries), prev_dir.name)
        return entries

    # --- Manifest methods ---

    def write_manifest(
        self, snapshot_dir: Path, filters: list, sieve_file: str,
        without_evidence: Optional[List[str]] = None,
        incomplete_excluded: Optional[List[ProtonMailFilter]] = None,
        incomplete_included: Optional[List[ProtonMailFilter]] = None,
    ):
        """Write manifest.json into a snapshot directory.

        `without_evidence` names the filters in the script that have no raw
        scrape evidence (backed up before format 1.1); `sync` refuses while
        it is non-empty. `incomplete_excluded` and `incomplete_included`
        are the filters not fully read when backed up that consolidate left
        out of the script, or put in under --allow-incomplete; each is
        recorded with its hash and scrape issues.
        """
        def describe(incomplete: Optional[List[ProtonMailFilter]]) -> List[dict]:
            """Name, hash and scrape issues of each incomplete filter."""
            return [
                {"name": f.name, "content_hash": f.content_hash, "scrape_issues": list(f.scrape_issues)}
                for f in incomplete or []
            ]

        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filter_hashes": sorted(set(f.content_hash for f in filters)),
            "filter_names": sorted(set(f.name for f in filters)),
            "filter_count": len(filters),
            "sieve_file": sieve_file,
            "without_evidence": sorted(set(without_evidence or [])),
            "incomplete_excluded": describe(incomplete_excluded),
            "incomplete_included": describe(incomplete_included),
            "synced_at": None,
        }
        manifest_path = snapshot_dir / "manifest.json"
        write_private_file(manifest_path, json.dumps(manifest, indent=2))
        logger.info("Manifest written: %s (%d filters)", manifest_path, len(filters))

    def load_manifest(self, snapshot_dir: Path) -> Optional[dict]:
        """Load manifest.json from a snapshot directory."""
        manifest_path = snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text())

    def promote_manifest(self, snapshot_dir: Path) -> bool:
        """Mark a manifest as synced by setting synced_at."""
        manifest = self.load_manifest(snapshot_dir)
        if manifest is None:
            return False
        manifest["synced_at"] = datetime.now(timezone.utc).isoformat()
        manifest_path = snapshot_dir / "manifest.json"
        write_private_file(manifest_path, json.dumps(manifest, indent=2))
        logger.info("Manifest promoted (synced): %s", manifest_path)
        return True

    def load_synced_hashes(self) -> Optional[set]:
        """Load filter content hashes from the latest synced manifest."""
        # Walk snapshots in reverse chronological order, find latest synced one
        for entry in sorted(self.snapshots_dir.iterdir(), reverse=True):
            if entry.name == "latest" or not entry.is_dir():
                continue
            manifest = self.load_manifest(entry)
            if manifest and manifest.get("synced_at"):
                return set(manifest.get("filter_hashes", []))
        return None
