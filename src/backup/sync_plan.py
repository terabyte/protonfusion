"""Decide which live UI filters `sync` may disable.

Disabling a UI filter is only safe when the script being uploaded carries
its rule. Anything else (a Sieve filter, a filter created or edited after
the backup, a row the scraper could not fully read, a filter whose rules
are not all in the script) keeps running, so mail handling never silently
stops for a rule that was never consolidated.

The script itself is the source of truth: every filter is checked against
the facts of the script being uploaded before it is disabled. The
consolidate manifest only narrows and explains the candidates; a manifest
that does not describe this script cannot cause a wrong disable.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import AbstractSet, Iterable, List, Optional, Set

from src.consolidator.carry_forward import filter_facts
from src.generator.sieve_rules import Fact
from src.models.filter_models import ProtonMailFilter

logger = logging.getLogger(__name__)


@dataclass
class DisablePlan:
    """The enabled live filters, sorted by what `sync` does with each."""

    # Wizard filters carried by the script and whose rules it holds: disabled
    to_disable: List[ProtonMailFilter] = field(default_factory=list)
    # Taken to be carried (manifest or backup) but some of its rules are not
    # in the script being uploaded, so disabling it would lose them
    not_covered: List[ProtonMailFilter] = field(default_factory=list)
    # Sieve filters (ProtonFusion's own included): never touched
    sieve: List[ProtonMailFilter] = field(default_factory=list)
    # Same content as a backed-up filter the script leaves out (--exclude, deprecated)
    not_in_script: List[ProtonMailFilter] = field(default_factory=list)
    # No backed-up filter has this content: created or edited after the backup
    after_backup: List[ProtonMailFilter] = field(default_factory=list)
    # The scrape could not read the whole filter, so its content is unknown
    unreadable: List[ProtonMailFilter] = field(default_factory=list)

    @property
    def left_enabled(self) -> List[ProtonMailFilter]:
        """Every enabled filter `sync` leaves running."""
        return self.sieve + self.not_covered + self.not_in_script + self.after_backup + self.unreadable


def check_disable_candidates(filters: Iterable[ProtonMailFilter]) -> None:
    """Raise ValueError if any filter is one `sync` must never disable.

    A filter the user had switched off must never be in the set sync
    disables: after a failed upload that set is switched back on, which
    would turn on a filter the user wanted off. A Sieve filter is never
    sync's to disable. Called on the plan and again at the call site, so a
    later change to either cannot quietly break the rule.
    """
    for f in filters:
        if not f.enabled:
            raise ValueError(f"refusing to disable '{f.name}': it was not enabled")
        if f.is_sieve:
            raise ValueError(f"refusing to disable '{f.name}': it is a Sieve filter")


def manifest_describes(manifest: Optional[dict], sieve_path: Path) -> bool:
    """True if the manifest was written for the script file at `sieve_path`.

    Paths are compared as the same file on disk when both exist (so a
    relative path, a symlink or a different spelling of the same path all
    match), else by their resolved absolute form. A relative path in the
    manifest resolves against the current directory, so run from elsewhere
    it simply does not match, which only costs the manifest's narrowing.
    """
    manifest_script = (manifest or {}).get("sieve_file")
    if not manifest_script:
        return False
    recorded = Path(manifest_script).expanduser()
    target = Path(sieve_path).expanduser()
    try:
        if recorded.exists() and target.exists():
            return os.path.samefile(recorded, target)
    except OSError:
        return False
    return recorded.resolve() == target.resolve()


def carried_hashes(
    manifest: Optional[dict], sieve_path: Path, backup_filters: Iterable[ProtonMailFilter],
) -> tuple[Set[str], bool]:
    """Content hashes of the filters taken to be carried by the uploaded script.

    The snapshot's manifest lists the filters `consolidate` put into its
    script, so it is used when that script is the one being uploaded.
    Otherwise (--sieve, a second consolidate --output) every wizard filter
    in the --backup snapshot is a candidate. Either way plan_disable still
    checks each one against the script's facts before disabling it.
    Returns (hashes, from_manifest).
    """
    if manifest_describes(manifest, sieve_path):
        return set(manifest.get("filter_hashes", [])), True
    return {f.content_hash for f in backup_filters if not f.is_sieve}, False


def rules_in_script(f: ProtonMailFilter, script_fact_set: AbstractSet[Fact]) -> bool:
    """True if every rule `f` generates is in the script's facts.

    A filter that generates no facts, or that cannot be generated at all,
    is never counted as in the script.
    """
    try:
        facts = filter_facts(f)
    except Exception as e:
        logger.warning("Could not generate the rules of filter '%s' to check them: %s", f.name, e)
        return False
    return bool(facts) and facts <= script_fact_set


def plan_disable(
    live_filters: Iterable[ProtonMailFilter],
    carried: Set[str],
    backup_filters: Iterable[ProtonMailFilter],
    sieve_filter_name: str,
    script_fact_set: AbstractSet[Fact],
) -> DisablePlan:
    """Sort the enabled live filters into what `sync` disables and what it leaves on.

    A filter is disabled only if it is a fully read wizard filter whose
    content_hash (name, logic, conditions, actions) is in `carried` AND
    whose generated rules are all among `script_fact_set`, the facts of
    the ProtonFusion section actually being uploaded. A name match alone
    is not enough: a filter edited after the backup has the same name but
    a rule the script does not hold.
    """
    backed_up = {f.content_hash for f in backup_filters}
    plan = DisablePlan()
    for f in live_filters:
        if not f.enabled:
            continue
        if f.is_sieve or f.name == sieve_filter_name:
            plan.sieve.append(f)
        elif not f.is_complete:
            plan.unreadable.append(f)
        elif f.content_hash in carried:
            if rules_in_script(f, script_fact_set):
                plan.to_disable.append(f)
            else:
                plan.not_covered.append(f)
        elif f.content_hash in backed_up:
            plan.not_in_script.append(f)
        else:
            plan.after_backup.append(f)
    check_disable_candidates(plan.to_disable)
    return plan
