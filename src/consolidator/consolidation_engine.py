"""Main consolidation engine that applies strategies to optimize filters."""

import logging
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field

from src.models.filter_models import ProtonMailFilter, ConsolidatedFilter, FilterStatus
from src.consolidator.strategies.group_by_action import group_by_action
from src.consolidator.strategies.merge_conditions import merge_conditions
from src.consolidator.strategies.optimize_ordering import optimize_ordering

logger = logging.getLogger(__name__)


def _action_target(action) -> str:
    """The folder or label an action points at, or "" if it has none."""
    return action.parameters.get("folder") or action.parameters.get("label") or ""


@dataclass
class ConsolidationReport:
    """Report showing consolidation results."""
    original_count: int = 0
    consolidated_count: int = 0
    enabled_count: int = 0
    disabled_skipped: int = 0
    disabled_included: int = 0
    archived_count: int = 0
    excluded_count: int = 0
    sieve_skipped: int = 0  # Sieve filters, never consolidated
    groups: Dict[str, int] = field(default_factory=dict)  # action -> count of merged filters
    reduction_percent: float = 0.0
    # Filters that would have been selected but were not fully read
    # (not is_complete). Left out unless allow_incomplete; listed either way.
    incomplete_excluded: List[ProtonMailFilter] = field(default_factory=list)
    incomplete_included: List[ProtonMailFilter] = field(default_factory=list)


@dataclass
class _Selection:
    """What _select_filters chose, and the counts of what it passed over."""
    selected: List[ProtonMailFilter] = field(default_factory=list)
    disabled_skipped: int = 0
    disabled_included: int = 0
    archived_count: int = 0
    excluded_count: int = 0
    sieve_skipped: int = 0
    incomplete_excluded: List[ProtonMailFilter] = field(default_factory=list)
    incomplete_included: List[ProtonMailFilter] = field(default_factory=list)


def _select_filters(
    filters: List[ProtonMailFilter],
    include_disabled: bool = False,
    synced_filter_hashes: Optional[Set[str]] = None,
    archived_filters: Optional[List[ProtonMailFilter]] = None,
    exclude_names: Optional[Set[str]] = None,
    allow_incomplete: bool = False,
) -> _Selection:
    """Select which filters to process based on status and sync manifest.

    Sieve filters are never selected: their conditions and actions are empty
    because the filter is a script, so consolidating one would emit an
    unconditional `keep;` that says nothing about what the script does.

    A filter that was not fully read (not is_complete) is left out unless
    allow_incomplete: what was read of it is only part of the rule, and a
    part can be wider than the whole. An AND filter missing one condition
    matches more mail, and one whose only condition was dropped matches all
    of it, so a delete action would delete everything.
    """
    _exclude_names = exclude_names or set()
    selection = _Selection()

    def take(f: ProtonMailFilter) -> bool:
        """Select f unless it is incomplete and not allowed; True if selected."""
        if f.is_complete:
            selection.selected.append(f)
            return True
        if allow_incomplete:
            selection.selected.append(f)
            selection.incomplete_included.append(f)
            return True
        selection.incomplete_excluded.append(f)
        return False

    # Always include archived filters from archive param
    _archived = archived_filters or []
    for f in _archived:
        if f.is_sieve:
            selection.sieve_skipped += 1
            continue
        if f.name in _exclude_names:
            selection.excluded_count += 1
            continue
        if f.status == FilterStatus.DEPRECATED:
            continue
        take(f)
    selection.archived_count = len(_archived)

    for f in filters:
        if f.is_sieve:
            selection.sieve_skipped += 1
            continue

        # DEPRECATED -> always skip
        if f.status == FilterStatus.DEPRECATED:
            continue

        # Skip if excluded by name
        if f.name in _exclude_names:
            selection.excluded_count += 1
            continue

        if include_disabled:
            if take(f) and not f.enabled:
                selection.disabled_included += 1
        elif f.enabled:
            take(f)
        elif synced_filter_hashes and f.content_hash in synced_filter_hashes:
            if take(f):
                selection.disabled_included += 1
        else:
            selection.disabled_skipped += 1

    return selection


class ConsolidationEngine:
    """Consolidates filters using composable strategies."""

    def consolidate(
        self,
        filters: List[ProtonMailFilter],
        include_disabled: bool = False,
        synced_filter_hashes: Optional[Set[str]] = None,
        archived_filters: Optional[List[ProtonMailFilter]] = None,
        exclude_names: Optional[Set[str]] = None,
        allow_incomplete: bool = False,
    ) -> tuple[List[ConsolidatedFilter], ConsolidationReport]:
        """Apply all consolidation strategies and return optimized filters + report.

        Incomplete filters are left out (and listed in the report) unless
        allow_incomplete; see _select_filters.
        """
        report = ConsolidationReport()
        report.original_count = len(filters)

        selection = _select_filters(
            filters, include_disabled, synced_filter_hashes, archived_filters, exclude_names,
            allow_incomplete=allow_incomplete,
        )
        selected = selection.selected
        report.enabled_count = len(selected)
        report.disabled_skipped = selection.disabled_skipped
        report.disabled_included = selection.disabled_included
        report.archived_count = selection.archived_count
        report.excluded_count = selection.excluded_count
        report.sieve_skipped = selection.sieve_skipped
        report.incomplete_excluded = selection.incomplete_excluded
        report.incomplete_included = selection.incomplete_included

        logger.info("Starting consolidation: %d total, %d selected, %d disabled-skipped, %d disabled-included, "
                    "%d archived, %d excluded, %d incomplete left out",
                    len(filters), len(selected), selection.disabled_skipped, selection.disabled_included,
                    selection.archived_count, selection.excluded_count, len(selection.incomplete_excluded))

        # Strategy 1: Group by action
        consolidated = group_by_action(selected)

        # Strategy 2: Merge similar conditions
        consolidated = merge_conditions(consolidated)

        # Strategy 3: Optimize ordering
        consolidated = optimize_ordering(consolidated)

        # Build report
        report.consolidated_count = len(consolidated)
        if report.original_count > 0:
            report.reduction_percent = (1 - report.consolidated_count / report.enabled_count) * 100 if report.enabled_count > 0 else 0

        for cf in consolidated:
            for action in cf.actions:
                action_desc = action.type.value
                if _action_target(action):
                    action_desc += f" ({_action_target(action)})"
                report.groups[action_desc] = report.groups.get(action_desc, 0) + cf.filter_count

        logger.info("Consolidation complete: %d -> %d filters (%.1f%% reduction)",
                     report.enabled_count, report.consolidated_count, report.reduction_percent)

        return consolidated, report

    def analyze(
        self,
        filters: List[ProtonMailFilter],
        include_disabled: bool = False,
        synced_filter_hashes: Optional[Set[str]] = None,
        archived_filters: Optional[List[ProtonMailFilter]] = None,
        exclude_names: Optional[Set[str]] = None,
    ) -> dict:
        """Analyze filters without consolidating. Returns statistics.

        Selects as consolidate does, so incomplete filters are not counted.
        """
        selection = _select_filters(
            filters, include_disabled, synced_filter_hashes, archived_filters, exclude_names,
        )
        selected = selection.selected
        disabled_included = selection.disabled_included
        disabled = len(filters) - len(selected)

        # Count by action type
        action_counts = {}
        for f in selected:
            for action in f.actions:
                key = action.type.value
                if _action_target(action):
                    key += f" -> {_action_target(action)}"
                action_counts[key] = action_counts.get(key, 0) + 1

        # Count by condition type
        condition_counts = {}
        for f in selected:
            for cond in f.conditions:
                condition_counts[cond.type.value] = condition_counts.get(cond.type.value, 0) + 1

        # Identify consolidation opportunities
        opportunities = {k: v for k, v in action_counts.items() if v > 1}

        return {
            "total_filters": len(filters),
            "enabled": len(selected),
            "disabled": disabled,
            "disabled_included": disabled_included,
            "action_distribution": dict(sorted(action_counts.items(), key=lambda x: -x[1])),
            "condition_distribution": dict(sorted(condition_counts.items(), key=lambda x: -x[1])),
            "consolidation_opportunities": dict(sorted(opportunities.items(), key=lambda x: -x[1])),
            "potential_reduction": len(selected) - len(opportunities) if opportunities else 0,
        }
