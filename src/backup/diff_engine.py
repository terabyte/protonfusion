"""Compare filter backups and current state."""

import logging
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field

from src.models.filter_models import ProtonMailFilter
from src.models.backup_models import Backup

logger = logging.getLogger(__name__)


@dataclass
class FilterDiff:
    """Represents differences between two filter states."""
    added: List[ProtonMailFilter] = field(default_factory=list)
    removed: List[ProtonMailFilter] = field(default_factory=list)
    modified: List[Tuple[ProtonMailFilter, ProtonMailFilter]] = field(default_factory=list)  # (old, new)
    state_changed: List[Tuple[ProtonMailFilter, ProtonMailFilter]] = field(default_factory=list)  # enabled/disabled changed
    unchanged: List[ProtonMailFilter] = field(default_factory=list)


class DiffEngine:
    """Compare filter states."""

    def compare_backups(self, backup1: Backup, backup2: Backup) -> FilterDiff:
        """Compare two backup files."""
        return self._compare_filter_lists(backup1.filters, backup2.filters)

    def compare_filter_lists(self, old_filters: List[ProtonMailFilter], new_filters: List[ProtonMailFilter]) -> FilterDiff:
        """Compare two lists of filters."""
        return self._compare_filter_lists(old_filters, new_filters)

    def _compare_filter_lists(self, old_filters: List[ProtonMailFilter], new_filters: List[ProtonMailFilter]) -> FilterDiff:
        """Core comparison logic."""
        diff = FilterDiff()

        old_by_name = {f.name: f for f in old_filters}
        new_by_name = {f.name: f for f in new_filters}

        old_names = set(old_by_name.keys())
        new_names = set(new_by_name.keys())

        # Added filters (in new but not in old)
        for name in new_names - old_names:
            diff.added.append(new_by_name[name])

        # Removed filters (in old but not in new)
        for name in old_names - new_names:
            diff.removed.append(old_by_name[name])

        # Compare common filters
        for name in old_names & new_names:
            old_f = old_by_name[name]
            new_f = new_by_name[name]

            # Check if only enabled state changed
            if old_f.enabled != new_f.enabled:
                if self._filters_equal_except_enabled(old_f, new_f):
                    diff.state_changed.append((old_f, new_f))
                else:
                    diff.modified.append((old_f, new_f))
            elif not self._filters_equal(old_f, new_f):
                diff.modified.append((old_f, new_f))
            else:
                diff.unchanged.append(old_f)

        return diff

    def _filters_equal(self, f1: ProtonMailFilter, f2: ProtonMailFilter) -> bool:
        """Check if two filters are identical."""
        return f1.model_dump() == f2.model_dump()

    def _filters_equal_except_enabled(self, f1: ProtonMailFilter, f2: ProtonMailFilter) -> bool:
        """Check if filters are identical except for enabled state."""
        d1 = f1.model_dump()
        d2 = f2.model_dump()
        d1.pop("enabled", None)
        d2.pop("enabled", None)
        return d1 == d2

    def generate_summary(self, diff: FilterDiff) -> dict:
        """Generate a summary of the diff."""
        return {
            "added": len(diff.added),
            "removed": len(diff.removed),
            "modified": len(diff.modified),
            "state_changed": len(diff.state_changed),
            "unchanged": len(diff.unchanged),
            "total_changes": len(diff.added) + len(diff.removed) + len(diff.modified) + len(diff.state_changed),
        }
