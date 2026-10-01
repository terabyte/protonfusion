"""Decide which live UI filters `sync` may disable.

Disabling a UI filter is only safe when the script being uploaded carries
its rule. Anything else (a Sieve filter, a filter created or edited after
the backup, a row the scraper could not fully read) keeps running, so mail
handling never silently stops for a rule that was never consolidated.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Set

from src.models.filter_models import ProtonMailFilter


@dataclass
class DisablePlan:
    """The enabled live filters, sorted by what `sync` does with each."""

    # Wizard filters whose content_hash is carried by the script: disabled
    to_disable: List[ProtonMailFilter] = field(default_factory=list)
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
        return self.sieve + self.not_in_script + self.after_backup + self.unreadable


def carried_hashes(
    manifest: Optional[dict], sieve_path: Path, backup_filters: Iterable[ProtonMailFilter],
) -> tuple[Set[str], bool]:
    """Content hashes of the filters whose rules the uploaded script carries.

    The snapshot's manifest lists exactly the filters `consolidate` put into
    its script, so it is used when that script is the one being uploaded.
    A script given with --sieve has no manifest, so every wizard filter in
    the --backup snapshot is taken as carried, which is what that option
    has always meant. Returns (hashes, from_manifest).
    """
    manifest_script = (manifest or {}).get("sieve_file")
    if manifest_script and Path(manifest_script).resolve() == sieve_path.resolve():
        return set(manifest.get("filter_hashes", [])), True
    return {f.content_hash for f in backup_filters if not f.is_sieve}, False


def plan_disable(
    live_filters: Iterable[ProtonMailFilter],
    carried: Set[str],
    backup_filters: Iterable[ProtonMailFilter],
    sieve_filter_name: str,
) -> DisablePlan:
    """Sort the enabled live filters into what `sync` disables and what it leaves on.

    A filter is disabled only if it is a fully read wizard filter whose
    content_hash (name, logic, conditions, actions) is in `carried`. A name
    match alone is not enough: a filter edited after the backup has the
    same name but a rule the script does not hold.
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
            plan.to_disable.append(f)
        elif f.content_hash in backed_up:
            plan.not_in_script.append(f)
        else:
            plan.after_backup.append(f)
    return plan
