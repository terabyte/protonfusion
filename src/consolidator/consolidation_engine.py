"""Main consolidation engine that applies strategies to optimize filters."""

import logging
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field

from src.models.filter_models import ProtonMailFilter, ConsolidatedFilter, FilterStatus
from src.consolidator.strategies.group_by_action import group_by_action
from src.consolidator.strategies.merge_conditions import merge_conditions
from src.consolidator.strategies.optimize_ordering import optimize_ordering

logger = logging.getLogger(__name__)


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
    groups: Dict[str, int] = field(default_factory=dict)  # action -> count of merged filters
    reduction_percent: float = 0.0


def _select_filters(
    filters: List[ProtonMailFilter],
    include_disabled: bool = False,
    synced_filter_hashes: Optional[Set[str]] = None,
    archived_filters: Optional[List[ProtonMailFilter]] = None,
    exclude_names: Optional[Set[str]] = None,
) -> tuple[List[ProtonMailFilter], int, int, int, int]:
    """Select which filters to process based on status and sync manifest.

    Returns (selected_filters, disabled_skipped, disabled_included, archived_count, excluded_count).
    """
    _exclude_names = exclude_names or set()
    selected = []
    disabled_skipped = 0
    disabled_included = 0
    excluded_count = 0

    # Always include archived filters from archive param
    _archived = archived_filters or []
    for f in _archived:
        if f.name in _exclude_names:
            excluded_count += 1
            continue
        if f.status == FilterStatus.DEPRECATED:
            continue
        selected.append(f)

    for f in filters:
        # DEPRECATED → always skip
        if f.status == FilterStatus.DEPRECATED:
            continue

        # Skip if excluded by name
        if f.name in _exclude_names:
            excluded_count += 1
            continue

        if include_disabled:
            selected.append(f)
            if not f.enabled:
                disabled_included += 1
        elif f.enabled:
            selected.append(f)
        elif synced_filter_hashes and f.content_hash in synced_filter_hashes:
            selected.append(f)
            disabled_included += 1
        else:
            disabled_skipped += 1

    return selected, disabled_skipped, disabled_included, len(_archived), excluded_count


class ConsolidationEngine:
    """Consolidates filters using composable strategies."""

    def consolidate(
        self,
        filters: List[ProtonMailFilter],
        include_disabled: bool = False,
        synced_filter_hashes: Optional[Set[str]] = None,
        archived_filters: Optional[List[ProtonMailFilter]] = None,
        exclude_names: Optional[Set[str]] = None,
    ) -> tuple[List[ConsolidatedFilter], ConsolidationReport]:
        """Apply all consolidation strategies and return optimized filters + report."""
        report = ConsolidationReport()
        report.original_count = len(filters)

        selected, disabled_skipped, disabled_included, archived_count, excluded_count = _select_filters(
            filters, include_disabled, synced_filter_hashes, archived_filters, exclude_names,
        )
        report.enabled_count = len(selected)
        report.disabled_skipped = disabled_skipped
        report.disabled_included = disabled_included
        report.archived_count = archived_count
        report.excluded_count = excluded_count

        logger.info("Starting consolidation: %d total, %d selected, %d disabled-skipped, %d disabled-included, %d archived, %d excluded",
                     len(filters), len(selected), disabled_skipped, disabled_included, archived_count, excluded_count)

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
                if action.parameters.get("folder"):
                    action_desc += f" ({action.parameters['folder']})"
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
        """Analyze filters without consolidating. Returns statistics."""
        selected, disabled_skipped, disabled_included, archived_count, excluded_count = _select_filters(
            filters, include_disabled, synced_filter_hashes, archived_filters, exclude_names,
        )
        disabled = len(filters) - len(selected)

        # Count by action type
        action_counts = {}
        for f in selected:
            for action in f.actions:
                key = action.type.value
                if action.parameters.get("folder"):
                    key += f" -> {action.parameters['folder']}"
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
