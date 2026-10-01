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

from src.backup.backup_manager import format_predates_strict_parser
from src.consolidator.carry_forward import filter_facts
from src.generator.sieve_rules import Fact, compare_sections, correct_legacy_actions, current_forms, script_facts
from src.models.backup_models import Backup
from src.models.filter_models import ProtonMailFilter, FilterStatus, legacy_identity
from src.scraper.protonmail_sync import ProtonMailSync
from src.utils.config import loggable_text

logger = logging.getLogger(__name__)

# (backed-up filter, live filter it was matched to)
Pair = Tuple[ProtonMailFilter, ProtonMailFilter]


def _group(filters: Iterable[ProtonMailFilter], key) -> Dict[str, List[ProtonMailFilter]]:
    """Group filters by key(filter), each group in row (priority) order."""
    groups: Dict[str, List[ProtonMailFilter]] = defaultdict(list)
    for f in sorted(filters, key=lambda f: f.priority):
        groups[key(f)].append(f)
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
    since the backup, so the restore cannot switch it back on.

    Legacy forms (the old `discard;` Trash action, a wildcard-less
    begins-with) count in both directions. A live fact in a legacy form is
    carried when a filter or the target script holds its current form, and,
    since the target script is often the older one, a live fact in its
    current form is carried when the target script holds a legacy form of
    it. compare_sections only pairs a legacy live form with a current new
    one, so the target side is expanded here.

    Raises SieveParseError if either script cannot be parsed.
    """
    dropped = compare_sections(live_script, script_active_after).dropped
    # Only the action fixes apply to the target side. A wildcard-less legacy
    # :matches in the target is an exact match, narrower than the live
    # "value*", so treating it as carrying the live rule would restore a rule
    # that handles less mail. current_forms' wildcard variants are only sound
    # from legacy live to corrected new, never the other way.
    in_target = set()
    if script_active_after:
        for fact in script_facts(script_active_after):
            in_target |= {fact, correct_legacy_actions(fact)}
    carried = set()
    for f in filters_on_after:
        if f.is_sieve or not f.is_complete:
            continue
        carried |= filter_facts(f)
    return [fact for fact in dropped if not (current_forms(fact) & (carried | in_target))]


class RestoreEngine:
    """Restore filter state from a backup."""

    def __init__(self, sync: ProtonMailSync):
        self.sync = sync
        # Filters whose switch was not seen to turn on after the click: how
        # ProtonMail refuses an enable at the account's active-filter limit
        self.enable_refused: List[str] = []
        # The pairs the last apply() call confirmed in the requested state, in
        # order: names can repeat, so a rollback needs the rows themselves.
        self.last_done: List[Pair] = []
        # The pairs the last apply() call may have changed, in order: those
        # confirmed, plus those clicked whose switch was not seen to change
        # (it may have changed late). What a rollback has to undo.
        self.last_touched: List[Pair] = []

    @staticmethod
    def plan(backup: Backup, current_filters: List[ProtonMailFilter]) -> RestorePlan:
        """Match each backed-up filter to a live row and decide what to toggle.

        A wizard filter is its content, so a filter edited since the backup,
        or a different filter that shares the name, never matches. Matching
        runs in two passes:

        1. By content_hash. Identical copies (same hash) are interchangeable,
           so equal counts pair in row order; unequal counts are ambiguous.
           Only for a backup in format 1.3 or later: older ones may store
           a filter in a legacy encoding, so they go straight to pass 2.
        2. Only for backed-up filters with no exact match, by
           legacy_identity, so a backup from before a format fix (", "-joined
           chips, unescaped "/", an old action type) still finds its live
           filter. That identity is coarser than content_hash (a literal
           "a, b" and the chips [a, b] share it), so a group is paired only
           when its members on each side all share one content_hash and the
           counts are equal. Anything else is ambiguous: pairing different
           filters by row order would toggle the wrong one after a reorder.

        A Sieve filter is matched by name: its script may legitimately differ
        (ProtonFusion's own is rewritten by every sync), and that must not
        make it unfindable. A name shared by several is ambiguous.
        """
        plan = RestorePlan()
        eligible = []
        for f in backup.filters:
            # Archived/deprecated filters are not on ProtonMail
            if f.status in (FilterStatus.ARCHIVED, FilterStatus.DEPRECATED):
                plan.skipped.append(f.name)
            else:
                eligible.append(f)

        def ambiguous(backed_up: List[ProtonMailFilter], reason: str) -> None:
            for f in backed_up:
                plan.ambiguous.append(f"{f.name}: {reason}")

        def count_mismatch(backed_up: List[ProtonMailFilter], live: List[ProtonMailFilter]) -> str:
            return (
                f"{len(backed_up)} in the backup and {len(live)} in the account with the "
                "same identity; cannot tell which is which"
            )

        pairs: List[Pair] = []

        # Sieve filters, by name
        live_sieve = _group((f for f in current_filters if f.is_sieve), lambda f: f.name)
        for name, backed_up in _group((f for f in eligible if f.is_sieve), lambda f: f.name).items():
            live = live_sieve.get(name, [])
            if not live:
                for f in backed_up:
                    plan.not_found.append(f"{f.name}: not in the account, or changed since the backup")
            elif len(live) > 1 or len(backed_up) > 1:
                ambiguous(backed_up, count_mismatch(backed_up, live))
            else:
                pairs.append((backed_up[0], live[0]))

        # Wizard filters, pass 1: exact content. Skipped for a backup older
        # than format 1.3, whose stored form may be a legacy encoding: its
        # literal "a, b" may be what are now the chips [a, b], so an exact
        # match with a live literal proves nothing.
        live_wizard = [f for f in current_filters if not f.is_sieve]
        live_by_hash = _group(live_wizard, lambda f: f.content_hash)
        claimed = set()  # ids of live filters an exact match took (or made ambiguous)
        unmatched: List[ProtonMailFilter] = []
        backed_wizard = [f for f in eligible if not f.is_sieve]
        if format_predates_strict_parser(backup.version):
            unmatched, backed_wizard = backed_wizard, []
        for h, backed_up in _group(backed_wizard, lambda f: f.content_hash).items():
            live = live_by_hash.get(h, [])
            if not live:
                unmatched.extend(backed_up)
                continue
            claimed |= {id(f) for f in live}
            if len(live) != len(backed_up):
                ambiguous(backed_up, count_mismatch(backed_up, live))
            else:
                pairs.extend(zip(backed_up, live))

        # Pass 2: legacy identity, for what had no exact match
        live_by_legacy = _group((f for f in live_wizard if id(f) not in claimed), legacy_identity)
        for key, backed_up in _group(unmatched, legacy_identity).items():
            live = live_by_legacy.get(key, [])
            if not live:
                for f in backed_up:
                    plan.not_found.append(f"{f.name}: not in the account, or changed since the backup")
            elif len(live) != len(backed_up):
                ambiguous(backed_up, count_mismatch(backed_up, live))
            elif len({f.content_hash for f in backed_up}) > 1 or len({f.content_hash for f in live}) > 1:
                ambiguous(
                    backed_up,
                    "several different filters in the backup or the account read the same once older "
                    "formats are normalised (a literal \", \" and separate value chips, for example); "
                    "cannot tell which is which",
                )
            else:
                pairs.extend(zip(backed_up, live))

        # In backup row order, the order the toggles are clicked in
        for backed, current in sorted(pairs, key=lambda pair: pair[0].priority):
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
        the wrong row, and a click whose switch was not seen to change counts
        as a failure, though the row lands in last_touched as possibly
        changed. Carries on past a failure. Returns (names done, error lines
        naming each failure); the pairs done are in last_done.
        """
        done, errors = [], []
        self.last_done = []
        self.last_touched = []
        verb = "enable" if enabled else "disable"
        for backed, live in pairs:
            try:
                if await self.sync.set_row_enabled(live.priority, live.name, enabled):
                    done.append(backed.name)
                    self.last_done.append((backed, live))
                    self.last_touched.append((backed, live))
                elif self.sync.last_toggle_refused:
                    errors.append(
                        f"{backed.name}: failed to {verb}: its switch was clicked but not seen to change "
                        "(it may have changed after the check)"
                    )
                    self.last_touched.append((backed, live))
                    if enabled:
                        self.enable_refused.append(backed.name)
                else:
                    errors.append(f"{backed.name}: failed to {verb} (row {live.priority} not found unambiguously)")
            except Exception as e:
                # The click may have happened, so the row may have changed
                self.last_touched.append((backed, live))
                # A Playwright error's call log can carry the page URL, whose
                # fragment holds a session key during Proton's fork.
                reason = loggable_text(str(e)) or type(e).__name__
                errors.append(f"{backed.name}: failed to {verb}: {reason}")
                logger.error("Error restoring filter '%s': %s", backed.name, reason)
        return done, errors
