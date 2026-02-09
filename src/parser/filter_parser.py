"""Parse scraped filter data into validated Pydantic models."""

import logging
from typing import List

from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction,
    ConditionType, Operator, ActionType, LogicType,
)

logger = logging.getLogger(__name__)

# Mapping of scraped strings to enum values
CONDITION_TYPE_MAP = {
    "sender": ConditionType.SENDER,
    "from": ConditionType.SENDER,
    "recipient": ConditionType.RECIPIENT,
    "to": ConditionType.RECIPIENT,
    "subject": ConditionType.SUBJECT,
    "attachments": ConditionType.ATTACHMENTS,
    "has attachment": ConditionType.ATTACHMENTS,
    "header": ConditionType.HEADER,
}

OPERATOR_MAP = {
    "contains": Operator.CONTAINS,
    "is": Operator.IS,
    "is exactly": Operator.IS,
    "matches": Operator.MATCHES,
    "starts with": Operator.STARTS_WITH,
    "ends with": Operator.ENDS_WITH,
    "has": Operator.HAS,
}

ACTION_TYPE_MAP = {
    "move to": ActionType.MOVE_TO,
    "move_to": ActionType.MOVE_TO,
    "move message to": ActionType.MOVE_TO,
    "apply label": ActionType.LABEL,
    "label": ActionType.LABEL,
    "mark as read": ActionType.MARK_READ,
    "mark_read": ActionType.MARK_READ,
    "star": ActionType.STAR,
    "star it": ActionType.STAR,
    "archive": ActionType.ARCHIVE,
    "move to archive": ActionType.ARCHIVE,
    "move to trash": ActionType.DELETE,
    "delete": ActionType.DELETE,
    "permanently delete": ActionType.DELETE,
}


def parse_condition_type(raw: str) -> ConditionType:
    """Map a scraped condition type string to enum."""
    normalized = raw.lower().strip()
    if normalized in CONDITION_TYPE_MAP:
        return CONDITION_TYPE_MAP[normalized]
    # Try partial match
    for key, val in CONDITION_TYPE_MAP.items():
        if key in normalized or normalized in key:
            return val
    logger.warning("Unknown condition type: '%s', defaulting to SENDER", raw)
    return ConditionType.SENDER


def parse_operator(raw: str) -> Operator:
    """Map a scraped operator string to enum."""
    normalized = raw.lower().strip()
    if normalized in OPERATOR_MAP:
        return OPERATOR_MAP[normalized]
    for key, val in OPERATOR_MAP.items():
        if key in normalized or normalized in key:
            return val
    logger.warning("Unknown operator: '%s', defaulting to CONTAINS", raw)
    return Operator.CONTAINS


def parse_action_type(raw: str) -> ActionType:
    """Map a scraped action type string to enum."""
    normalized = raw.lower().strip()
    if normalized in ACTION_TYPE_MAP:
        return ACTION_TYPE_MAP[normalized]
    for key, val in ACTION_TYPE_MAP.items():
        if key in normalized or normalized in key:
            return val
    logger.warning("Unknown action type: '%s', defaulting to MOVE_TO", raw)
    return ActionType.MOVE_TO


def parse_filter(raw: dict) -> ProtonMailFilter:
    """Parse a single scraped filter dict into a ProtonMailFilter model."""
    conditions = []
    for cond in raw.get("conditions", []):
        conditions.append(FilterCondition(
            type=parse_condition_type(cond.get("type", "sender")),
            operator=parse_operator(cond.get("operator", "contains")),
            value=cond.get("value", ""),
        ))

    actions = []
    for act in raw.get("actions", []):
        actions.append(FilterAction(
            type=parse_action_type(act.get("type", "move_to")),
            parameters=act.get("parameters", {}),
        ))

    logic_str = raw.get("logic", "and").lower()
    logic = LogicType.OR if logic_str == "or" else LogicType.AND

    return ProtonMailFilter(
        name=raw.get("name", "Unknown Filter"),
        enabled=raw.get("enabled", True),
        priority=raw.get("priority", 0),
        logic=logic,
        conditions=conditions,
        actions=actions,
    )


def parse_scraped_filters(raw_filters: List[dict]) -> List[ProtonMailFilter]:
    """Parse a list of scraped filter dicts into validated models."""
    parsed = []
    for raw in raw_filters:
        try:
            f = parse_filter(raw)
            parsed.append(f)
        except Exception as e:
            name = raw.get("name", "?") if isinstance(raw, dict) else "?"
            logger.warning("Failed to parse filter '%s': %s", name, e)
    logger.info("Parsed %d/%d filters successfully", len(parsed), len(raw_filters))
    return parsed
