"""Strategy: Merge similar conditions into array format."""

import logging
from typing import List, Dict

from src.models.filter_models import (
    ConsolidatedFilter, FilterCondition, ConditionType, Operator,
)

logger = logging.getLogger(__name__)


def merge_conditions(filters: List[ConsolidatedFilter]) -> List[ConsolidatedFilter]:
    """Merge conditions of the same type+operator into combined conditions.

    For example, 3 conditions checking "sender contains X" become one check
    with multiple values (used for Sieve array syntax).
    """
    result = []

    for f in filters:
        if len(f.conditions) <= 1:
            result.append(f)
            continue

        # Group conditions by type + operator
        groups: Dict[str, List[FilterCondition]] = {}
        for cond in f.conditions:
            key = f"{cond.type.value}|{cond.operator.value}"
            if key not in groups:
                groups[key] = []
            groups[key].append(cond)

        # Create merged conditions
        merged_conditions = []
        for key, conds in groups.items():
            if len(conds) == 1:
                merged_conditions.append(conds[0])
            else:
                # Combine values - store as comma-separated for Sieve array generation
                values = [c.value for c in conds if c.value]
                merged_conditions.append(FilterCondition(
                    type=conds[0].type,
                    operator=conds[0].operator,
                    value="|".join(values),  # Pipe-delimited for array expansion
                ))

        result.append(ConsolidatedFilter(
            name=f.name,
            logic=f.logic,
            conditions=merged_conditions,
            actions=f.actions,
            source_filters=f.source_filters,
            filter_count=f.filter_count,
        ))

    logger.info("Merged conditions in %d consolidated filters", len(result))
    return result
