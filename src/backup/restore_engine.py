"""Plan and apply the filter half of a restore: UI filters' enabled/disabled state.

The `restore` command also puts back the Sieve script captured in the
backup; that half lives in the command, which orders the two (see
src/main.py `restore`). This module only toggles filters on or off: it
never creates, deletes or edits one.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

from src.consolidator.carry_forward import filter_facts
from src.generator.sieve_rules import Fact, compare_sections, current_forms
from src.models.backup_models import Backup
from src.models.filter_models import ProtonMailFilter, FilterStatus, legacy_identity
from src.scraper.protonmail_sync import ProtonMailSync
from src.utils.config import loggable_text

logger = logging.getLogger(__name__)

# (backed-up filter, live filter it was matched to)
Pair = Tuple[ProtonMailFilter, ProtonMailFilter]


def _identity(f: ProtonMailFilter) -> Tuple[str, str]:
    """The key a live filter must share with its backup copy to be the same filter.

    A wizard filter is its content (content_hash covers name, logic,
    conditions and actions), so a filter edited since the backup, or a
    different filter that happens to share the name, never matches. A
    Sieve filter is matched by name: its script may legitimately differ
    (ProtonFusion's own is rewritten by every sync), and that must not make
    it unfindable.
    """
    if f.is_sieve:
        return ("sieve", f.name)
    # legacy_identity, not content_hash: a backup from before a format fix
    # (", "-joined chips, unescaped "/") must still match its live filter.
    return ("content", legacy_identity(f))


def _by_identity(filters: List[ProtonMailFilter]) -> Dict[Tuple[str, str], List[ProtonMailFilter]]:
    """Group filters by _identity, each group in row (priority) order."""
    groups: Dict[Tuple[str, str], List[ProtonMailFilter]] = defaultdict(list)
    for f in sorted(filters, key=lambda f: f.priority):
        groups[_identity(f)].append(f)
    return groups


@dataclass
class RestorePlan:
    """What restoring a backup's filter states would change, decided before any click.

    to_enable / to_disable pair each backed-up filter with the live row it
    was matched to. The lists of strings carry a name and, for the ones
    that cannot be restored, the reason.
    """
    to_enable: List[Pair] = field(default_factory=list)
    to_disable: List[Pair] = field(default_factory=list)
    already_correct: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)      # archived/deprecated, not on ProtonMail
    not_found: List[str] = field(default_factory=list)
    ambiguous: List[str] = field(default_factory=list)
    script_differs: List[str] = field(default_factory=list)  # Sieve filters whose script changed

    @property
    def unrestorable(self) -> List[str]:
        """Every backed-up filter this plan cannot put back, with the reason."""
        return self.not_found + self.ambiguous


def uncovered_live_rules(
    live_script: str, script_active_after: str, filters_on_after: Iterable[ProtonMailFilter],
) -> List[Fact]:
    """Rules of the live ProtonFusion section that nothing would carry after the restore.

    A restore replaces (or switches off) ProtonFusion's script, so every
    fact of its live section must end up either in `script_active_after`
    (the script that filters mail once the restore is done: the backed-up
    one, the live one if it is unchanged, or "" if the filter ends off) or
    in a wizard filter that is on afterwards. A fact in neither is mail the
    restore leaves unfiltered: a filter `cleanup` deleted, or one edited
    since the backup, so the restore cannot switch it back on. A legacy
    form of a fact (old Trash action, wildcard-less begins-with) counts as
    carried when its current form is.

    Raises SieveParseError if either script cannot be parsed.
    """
    dropped = compare_sections(live_script, script_active_after).dropped
    carried = set()
    for f in filters_on_after:
        if f.is_sieve or not f.is_complete:
            continue
        carried |= filter_facts(f)
    return [fact for fact in dropped if not (current_forms(fact) & carried)]


class RestoreEngine:
    """Restore filter state from a backup."""

    def __init__(self, sync: ProtonMailSync):
        self.sync = sync
        # Filters whose switch did not turn on when clicked: how ProtonMail
        # refuses an enable at the account's active-filter limit
        self.enable_refused: List[str] = []

    @staticmethod
    def plan(backup: Backup, current_filters: List[ProtonMailFilter]) -> RestorePlan:
        """Match each backed-up filter to a live row and decide what to toggle.

        Pairs by _identity. Identical copies (same content) pair in row
        order when the backup and the account hold the same number of
        them; any other mismatch in count is ambiguous and nothing in that
        group is touched, as is a Sieve name shared by several filters.
        """
        plan = RestorePlan()
        eligible = []
        for f in backup.filters:
            # Archived/deprecated filters are not on ProtonMail
            if f.status in (FilterStatus.ARCHIVED, FilterStatus.DEPRECATED):
                plan.skipped.append(f.name)
            else:
                eligible.append(f)

        current_groups = _by_identity(current_filters)
        for key, backed_up in _by_identity(eligible).items():
            live = current_groups.get(key, [])
            if not live:
                for f in backed_up:
                    plan.not_found.append(f"{f.name}: not in the account, or changed since the backup")
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
                    plan.ambiguous.append(f"{f.name}: {reason}")
                continue
            for backed, current in zip(backed_up, live):
                if backed.is_sieve and backed.content_hash != current.content_hash:
                    plan.script_differs.append(backed.name)
                if backed.enabled == current.enabled:
                    plan.already_correct.append(backed.name)
                elif backed.enabled:
                    plan.to_enable.append((backed, current))
                else:
                    plan.to_disable.append((backed, current))
        return plan

    async def apply(self, pairs: List[Pair], enabled: bool) -> Tuple[List[str], List[str]]:
        """Set each matched live row to `enabled`, by row position confirmed by name.

        Uses ProtonMailSync.set_row_enabled, so a shared name never toggles
        the wrong row, and a click that did not change the switch counts as
        a failure. Carries on past a failure. Returns (names done,
        error lines naming each failure).
        """
        done, errors = [], []
        verb = "enable" if enabled else "disable"
        for backed, live in pairs:
            try:
                if await self.sync.set_row_enabled(live.priority, live.name, enabled):
                    done.append(backed.name)
                elif self.sync.last_toggle_refused:
                    errors.append(f"{backed.name}: failed to {verb}: its switch did not change when clicked")
                    if enabled:
                        self.enable_refused.append(backed.name)
                else:
                    errors.append(f"{backed.name}: failed to {verb} (row {live.priority} not found unambiguously)")
            except Exception as e:
                # A Playwright error's call log can carry the page URL, whose
                # fragment holds a session key during Proton's fork.
                reason = loggable_text(str(e)) or type(e).__name__
                errors.append(f"{backed.name}: failed to {verb}: {reason}")
                logger.error("Error restoring filter '%s': %s", backed.name, reason)
        return done, errors

    async def restore_from_backup(self, backup: Backup, current_filters: List[ProtonMailFilter]) -> dict:
        """Plan, then enable before disabling, and return a report dict.

        Enabling first means a failure among the enables has switched
        nothing off yet. Report keys (lists): enabled, disabled,
        already_correct, skipped, not_found, ambiguous, errors,
        script_not_restored.
        """
        plan = self.plan(backup, current_filters)
        enabled, enable_errors = await self.apply(plan.to_enable, True)
        disabled, disable_errors = await self.apply(plan.to_disable, False)
        report = {
            "enabled": enabled,
            "disabled": disabled,
            "skipped": plan.skipped,
            "not_found": plan.not_found,
            "ambiguous": plan.ambiguous,
            "already_correct": plan.already_correct,
            "errors": enable_errors + disable_errors,
            "script_not_restored": plan.script_differs,
        }
        logger.info(
            "Restore complete: %d enabled, %d disabled, %d not found, %d ambiguous, "
            "%d already correct, %d errors",
            len(enabled), len(disabled), len(plan.not_found), len(plan.ambiguous),
            len(plan.already_correct), len(report["errors"]),
        )
        return report
