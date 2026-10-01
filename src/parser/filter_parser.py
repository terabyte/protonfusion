"""Parse scraped filter data into validated Pydantic models."""

import logging
from typing import List, Optional

from src.models.filter_models import (
    ProtonMailFilter, ConditionType, Operator, ActionType, LogicType,
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
    "starts_with": Operator.STARTS_WITH,
    "ends_with": Operator.ENDS_WITH,
    "begins with": Operator.STARTS_WITH,
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
    # Trash is a folder move: the mail stays recoverable. "delete" is the
    # name older backups used for it (see LEGACY_ACTION_TYPES). There is no
    # permanent-delete entry because Proton's wizard has no such action.
    "trash": ActionType.TRASH,
    "move to trash": ActionType.TRASH,
    "delete": ActionType.TRASH,
}

LOGIC_MAP = {
    "and": LogicType.AND,
    "or": LogicType.OR,
}


class UnknownFilterValueError(ValueError):
    """A scraped condition/action field is missing or holds an unrecognised value.

    Raised instead of guessing: mapping an unknown condition to "sender
    contains" or an unknown action to "move to" can widen a rule, and a
    delete rule that matches more mail deletes more mail.
    """

    def __init__(self, field: str, value, filter_name: Optional[str] = None):
        self.field = field
        self.value = value
        self.filter_name = filter_name
        problem = f"missing {field}" if value is None else f"unknown {field} {value!r}"
        prefix = f"filter {filter_name!r}: " if filter_name is not None else ""
        super().__init__(prefix + problem)


def _lookup(raw, mapping: dict, field: str):
    """Map a scraped string to an enum value by exact (normalised) match only.

    No partial matching: "is not" contains "is" and "does not contain"
    contains "contains", so a substring match can invert a condition.
    """
    if not isinstance(raw, str):
        raise UnknownFilterValueError(field, raw)
    normalized = raw.lower().strip()
    if normalized in mapping:
        return mapping[normalized]
    raise UnknownFilterValueError(field, raw)


def parse_condition_type(raw) -> ConditionType:
    """Map a scraped condition type string to enum; raise UnknownFilterValueError if unknown."""
    return _lookup(raw, CONDITION_TYPE_MAP, "condition type")


def parse_operator(raw) -> Operator:
    """Map a scraped operator string to enum; raise UnknownFilterValueError if unknown."""
    return _lookup(raw, OPERATOR_MAP, "operator")


def parse_action_type(raw) -> ActionType:
    """Map a scraped action type string to enum; raise UnknownFilterValueError if unknown."""
    return _lookup(raw, ACTION_TYPE_MAP, "action type")


def parse_logic(raw) -> LogicType:
    """Map a scraped logic string to enum. Missing means AND, the narrower reading."""
    if raw is None:
        return LogicType.AND
    return _lookup(raw, LOGIC_MAP, "logic")


def _parse_or_keep(parse, raw, name: str, strict: bool):
    """Run one field parser. On an unknown value, raise naming the filter
    (strict) or return the raw value for the model to flag (not strict)."""
    try:
        return parse(raw)
    except UnknownFilterValueError as e:
        if strict:
            raise UnknownFilterValueError(e.field, e.value, filter_name=name) from None
        return raw


def parse_filter(raw: dict, strict: bool = True) -> ProtonMailFilter:
    """Parse a single scraped filter dict into a ProtonMailFilter model.

    A condition or action whose type/operator is missing or unrecognised
    raises UnknownFilterValueError naming the filter and the value. With
    strict=False it is passed through unmapped instead, and the model drops
    it and records it in scrape_issues, so the filter comes back flagged
    incomplete rather than with a guessed (possibly wider) rule.
    """
    name = raw.get("name", "Unknown Filter")

    conditions = []
    for cond in raw.get("conditions", []):
        parsed = {
            "type": _parse_or_keep(parse_condition_type, cond.get("type"), name, strict),
            "operator": _parse_or_keep(parse_operator, cond.get("operator"), name, strict),
            "value": cond.get("value", ""),
        }
        # Several wizard chips arrive as a list; a single value is a literal
        if "values" in cond:
            parsed["values"] = cond["values"]
        conditions.append(parsed)

    actions = []
    for act in raw.get("actions", []):
        actions.append({
            "type": _parse_or_keep(parse_action_type, act.get("type"), name, strict),
            "parameters": act.get("parameters", {}),
        })

    logic = _parse_or_keep(parse_logic, raw.get("logic"), name, strict)

    # Left out when absent so the model derives it from raw.sieve_text
    extra = {"is_sieve": raw["is_sieve"]} if "is_sieve" in raw else {}

    return ProtonMailFilter(
        name=name,
        enabled=raw.get("enabled", True),
        priority=raw.get("priority", 0),
        logic=logic,
        conditions=conditions,
        actions=actions,
        raw=raw.get("raw"),
        scrape_issues=list(raw.get("scrape_issues", [])),
        **extra,
    )


def parse_scraped_filters(raw_filters: List[dict]) -> List[ProtonMailFilter]:
    """Parse a list of scraped filter dicts into validated models.

    Unknown or missing condition/action values do not drop the filter: it is
    kept, flagged incomplete with the bad entry in scrape_issues (see
    parse_filter), so backup refuses it without --allow-incomplete,
    consolidate leaves it out and cleanup will not delete it.
    """
    parsed = []
    for raw in raw_filters:
        try:
            f = parse_filter(raw, strict=False)
            parsed.append(f)
        except Exception as e:
            name = raw.get("name", "?") if isinstance(raw, dict) else "?"
            logger.warning("Failed to parse filter '%s': %s", name, e)
    logger.info("Parsed %d/%d filters successfully", len(parsed), len(raw_filters))
    return parsed
