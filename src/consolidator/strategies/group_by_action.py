"""Strategy: Group filters by their action and merge into condition groups."""

import logging
from typing import List, Dict

from src.models.filter_models import (
    ProtonMailFilter, ConsolidatedFilter, ConditionGroup,
    FilterAction, FilterCondition, LogicType, ActionType,
)

logger = logging.getLogger(__name__)


def _action_key(action: FilterAction) -> str:
    """Create a hashable key for an action."""
    params_key = ":".join(f"{k}={v}" for k, v in sorted(action.parameters.items()))
    return f"{action.type.value}|{params_key}"


def _actions_key(actions: List[FilterAction]) -> str:
    """Create a hashable key for a list of actions."""
    return "||".join(sorted(_action_key(a) for a in actions))


def group_by_action(filters: List[ProtonMailFilter]) -> List[ConsolidatedFilter]:
    """Group filters that have the same action(s) into one consolidated filter.

    Each original filter's conditions become a ConditionGroup that preserves
    the original logic (AND/OR). Groups are OR'd together in the consolidated
    filter, so any original filter matching will trigger the action.

    This preserves exact behavior: a filter with "sender=X AND subject=Y"
    stays as an AND group, and is NOT flattened into an OR with other conditions.
    """
    # Group filters by their action key
    groups: Dict[str, List[ProtonMailFilter]] = {}

    for f in filters:
        if not f.enabled:
            continue
        key = _actions_key(f.actions)
        if key not in groups:
            groups[key] = []
        groups[key].append(f)

    consolidated = []

    for action_key, group in groups.items():
        if len(group) == 1:
            f = group[0]
            consolidated.append(ConsolidatedFilter(
                name=f.name,
                condition_groups=[ConditionGroup(logic=f.logic, conditions=f.conditions)],
                actions=f.actions,
                source_filters=[f.name],
                filter_count=1,
            ))
        else:
            # Each filter becomes its own condition group, preserving its logic
            condition_groups = []
            source_names = []

            for f in group:
                condition_groups.append(ConditionGroup(
                    logic=f.logic,
                    conditions=f.conditions,
                ))
                source_names.append(f.name)

            action_desc = _describe_actions(group[0].actions)
            name = f"{action_desc} (consolidated from {len(group)} filters)"

            consolidated.append(ConsolidatedFilter(
                name=name,
                condition_groups=condition_groups,
                actions=group[0].actions,
                source_filters=source_names,
                filter_count=len(group),
            ))

    logger.info("Grouped %d filters into %d consolidated filters", len(filters), len(consolidated))
    return consolidated


def _describe_actions(actions: List[FilterAction]) -> str:
    """Create a human-readable description of actions."""
    parts = []
    for a in actions:
        if a.type == ActionType.MOVE_TO:
            folder = a.parameters.get("folder", "?")
            parts.append(f"Move to {folder}")
        elif a.type == ActionType.LABEL:
            label = a.parameters.get("label", a.parameters.get("folder", "?"))
            parts.append(f"Label {label}")
        elif a.type == ActionType.MARK_READ:
            parts.append("Mark as read")
        elif a.type == ActionType.STAR:
            parts.append("Star")
        elif a.type == ActionType.ARCHIVE:
            parts.append("Archive")
        elif a.type == ActionType.DELETE:
            parts.append("Delete")
        else:
            parts.append(str(a.type.value))
    return " + ".join(parts) if parts else "Unknown action"
