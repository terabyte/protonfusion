"""Strategy: Merge compatible single-condition groups into array format."""

import logging
from typing import List, Dict

from src.models.filter_models import (
    ConsolidatedFilter, ConditionGroup, FilterCondition, ConditionType, Operator,
)

logger = logging.getLogger(__name__)


def merge_conditions(filters: List[ConsolidatedFilter]) -> List[ConsolidatedFilter]:
    """Merge compatible condition groups within each consolidated filter.

    Only single-condition groups with the same type and operator can be merged.
    Multi-condition groups (AND/OR with multiple conditions) are never merged
    because that would change the filter's behavior.

    Example (safe to merge):
        Group 1: sender contains "alice"    (single condition)
        Group 2: sender contains "bob"      (single condition)
        → Merged: sender contains ["alice", "bob"]  (one group, array values)

    Example (NOT merged):
        Group 1: sender contains "alice" AND subject contains "urgent"
        Group 2: sender contains "bob"
        → Left as-is (two separate groups)
    """
    result = []

    for f in filters:
        if len(f.condition_groups) <= 1:
            result.append(f)
            continue

        # Separate single-condition groups from multi-condition groups
        single_groups: Dict[str, List[ConditionGroup]] = {}
        multi_groups: List[ConditionGroup] = []

        for group in f.condition_groups:
            if len(group.conditions) == 1:
                cond = group.conditions[0]
                key = f"{cond.type.value}|{cond.operator.value}"
                if key not in single_groups:
                    single_groups[key] = []
                single_groups[key].append(group)
            else:
                # Multi-condition group: preserve as-is
                multi_groups.append(group)

        # Merge compatible single-condition groups
        merged_groups = []
        for key, groups in single_groups.items():
            if len(groups) == 1:
                merged_groups.append(groups[0])
            else:
                # Combine values with pipe delimiter for Sieve array expansion
                values = [g.conditions[0].value for g in groups if g.conditions[0].value]
                merged_groups.append(ConditionGroup(
                    logic=groups[0].logic,
                    conditions=[FilterCondition(
                        type=groups[0].conditions[0].type,
                        operator=groups[0].conditions[0].operator,
                        value="|".join(values),
                    )],
                ))

        result.append(ConsolidatedFilter(
            name=f.name,
            condition_groups=merged_groups + multi_groups,
            actions=f.actions,
            source_filters=f.source_filters,
            filter_count=f.filter_count,
        ))

    logger.info("Merged conditions in %d consolidated filters", len(result))
    return result
