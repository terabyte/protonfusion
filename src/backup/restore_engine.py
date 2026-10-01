"""Restore filters' enabled/disabled state from a backup.

Restore only toggles UI filters on or off to match the backup. It does not
create, delete or edit filters, and it never changes a Sieve script: the
script as it was at backup time is kept in backup.json's `sieve_script`.
"""

import logging
from collections import defaultdict
from typing import Dict, List, Tuple

from src.models.backup_models import Backup
from src.models.filter_models import ProtonMailFilter, FilterStatus
from src.scraper.protonmail_sync import ProtonMailSync

logger = logging.getLogger(__name__)


def _identity(f: ProtonMailFilter) -> Tuple[str, str]:
    """The key a live filter must share with its backup copy to be the same filter.

    A wizard filter is its content (content_hash covers name, logic,
    conditions and actions), so a filter edited since the backup, or a
    different filter that happens to share the name, never matches. A
    Sieve filter is matched by name: its script is exactly what restore
    does not restore, so a changed script must not make it unfindable.
    """
    if f.is_sieve:
        return ("sieve", f.name)
    return ("content", f.content_hash)


def _by_identity(filters: List[ProtonMailFilter]) -> Dict[Tuple[str, str], List[ProtonMailFilter]]:
    """Group filters by _identity, each group in row (priority) order."""
    groups: Dict[Tuple[str, str], List[ProtonMailFilter]] = defaultdict(list)
    for f in sorted(filters, key=lambda f: f.priority):
        groups[_identity(f)].append(f)
    return groups


class RestoreEngine:
    """Restore filter state from a backup."""

    def __init__(self, sync: ProtonMailSync):
        self.sync = sync

    async def restore_from_backup(self, backup: Backup, current_filters: List[ProtonMailFilter]) -> dict:
        """Enable or disable each live filter to match its state in the backup.

        Each backup filter is paired with the live filter that has the same
        identity (see _identity), and the toggle is set on that live row by
        position plus name (ProtonMailSync.set_row_enabled), so a duplicate
        name never toggles the wrong row. Identical copies (same content)
        are paired in row order when the backup and the account hold the
        same number of them; any other mismatch in count is ambiguous and
        nothing in that group is touched.

        Returns a report dict of lists of filter names (errors and the
        not-restored lists carry a reason): enabled, disabled,
        already_correct, skipped (archived/deprecated, not on ProtonMail),
        not_found, ambiguous, errors, and script_not_restored (Sieve filters
        whose script differs from the backup; their toggle is still set).
        """
        report = {
            "enabled": [],
            "disabled": [],
            "skipped": [],
            "not_found": [],
            "ambiguous": [],
            "already_correct": [],
            "errors": [],
            "script_not_restored": [],
        }

        eligible = []
        for f in backup.filters:
            # Archived/deprecated filters are not on ProtonMail
            if f.status in (FilterStatus.ARCHIVED, FilterStatus.DEPRECATED):
                report["skipped"].append(f.name)
            else:
                eligible.append(f)

        current_groups = _by_identity(current_filters)
        pairs: List[Tuple[ProtonMailFilter, ProtonMailFilter]] = []
        for key, backed_up in _by_identity(eligible).items():
            live = current_groups.get(key, [])
            if not live:
                reason = "not in the account, or changed since the backup"
                for f in backed_up:
                    report["not_found"].append(f"{f.name}: {reason}")
                    logger.warning("Filter '%s' %s", f.name, reason)
                continue
            # A Sieve filter is matched by name alone, so pairing several by
            # order would be a guess; identical wizard filters are
            # interchangeable, so equal counts pair safely.
            if len(live) != len(backed_up) or (key[0] == "sieve" and len(live) > 1):
                reason = (
                    f"{len(backed_up)} in the backup and {len(live)} in the account with the "
                    "same identity; cannot tell which is which"
                )
                for f in backed_up:
                    report["ambiguous"].append(f"{f.name}: {reason}")
                    logger.warning("Filter '%s': %s", f.name, reason)
                continue
            pairs.extend(zip(backed_up, live))

        for backed_up, live in pairs:
            name = backed_up.name
            if backed_up.is_sieve and backed_up.content_hash != live.content_hash:
                report["script_not_restored"].append(name)
            if backed_up.enabled == live.enabled:
                report["already_correct"].append(name)
                continue
            try:
                if await self.sync.set_row_enabled(live.priority, live.name, backed_up.enabled):
                    report["enabled" if backed_up.enabled else "disabled"].append(name)
                else:
                    verb = "enable" if backed_up.enabled else "disable"
                    report["errors"].append(f"{name}: failed to {verb} (row {live.priority} not found unambiguously)")
            except Exception as e:
                report["errors"].append(f"{name}: {e}")
                logger.error("Error restoring filter '%s': %s", name, e)

        logger.info(
            "Restore complete: %d enabled, %d disabled, %d not found, %d ambiguous, "
            "%d already correct, %d errors",
            len(report["enabled"]), len(report["disabled"]), len(report["not_found"]),
            len(report["ambiguous"]), len(report["already_correct"]), len(report["errors"]),
        )
        return report
