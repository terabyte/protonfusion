"""Main consolidation engine that applies strategies to optimize filters."""

import logging
from typing import List, Dict
from dataclasses import dataclass, field

from src.models.filter_models import ProtonMailFilter, ConsolidatedFilter
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
    groups: Dict[str, int] = field(default_factory=dict)  # action -> count of merged filters
    reduction_percent: float = 0.0


class ConsolidationEngine:
    """Consolidates filters using composable strategies."""

    def consolidate(self, filters: List[ProtonMailFilter]) -> tuple[List[ConsolidatedFilter], ConsolidationReport]:
        """Apply all consolidation strategies and return optimized filters + report."""
        report = ConsolidationReport()
        report.original_count = len(filters)

        # Count enabled/disabled
        enabled = [f for f in filters if f.enabled]
        disabled = [f for f in filters if not f.enabled]
        report.enabled_count = len(enabled)
        report.disabled_skipped = len(disabled)

        logger.info("Starting consolidation: %d total, %d enabled, %d disabled",
                     len(filters), len(enabled), len(disabled))

        # Strategy 1: Group by action
        consolidated = group_by_action(enabled)

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

    def analyze(self, filters: List[ProtonMailFilter]) -> dict:
        """Analyze filters without consolidating. Returns statistics."""
        enabled = [f for f in filters if f.enabled]
        disabled = [f for f in filters if not f.enabled]

        # Count by action type
        action_counts = {}
        for f in enabled:
            for action in f.actions:
                key = action.type.value
                if action.parameters.get("folder"):
                    key += f" -> {action.parameters['folder']}"
                action_counts[key] = action_counts.get(key, 0) + 1

        # Count by condition type
        condition_counts = {}
        for f in enabled:
            for cond in f.conditions:
                condition_counts[cond.type.value] = condition_counts.get(cond.type.value, 0) + 1

        # Identify consolidation opportunities
        opportunities = {k: v for k, v in action_counts.items() if v > 1}

        return {
            "total_filters": len(filters),
            "enabled": len(enabled),
            "disabled": len(disabled),
            "action_distribution": dict(sorted(action_counts.items(), key=lambda x: -x[1])),
            "condition_distribution": dict(sorted(condition_counts.items(), key=lambda x: -x[1])),
            "consolidation_opportunities": dict(sorted(opportunities.items(), key=lambda x: -x[1])),
            "potential_reduction": len(enabled) - len(opportunities) if opportunities else 0,
        }
