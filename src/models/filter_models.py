import hashlib
import json
import logging
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, model_serializer, model_validator


class ConditionType(str, Enum):
    SENDER = "sender"
    RECIPIENT = "recipient"
    SUBJECT = "subject"
    ATTACHMENTS = "attachments"
    HEADER = "header"


class Operator(str, Enum):
    CONTAINS = "contains"
    IS = "is"
    MATCHES = "matches"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    HAS = "has"  # for attachments


class ActionType(str, Enum):
    MOVE_TO = "move_to"
    LABEL = "label"
    MARK_READ = "mark_read"
    STAR = "star"
    ARCHIVE = "archive"
    # "Move to Trash" in the wizard's folder dropdown. Generated as
    # `fileinto "trash";`: the mail stays recoverable from Trash. Proton's
    # filter wizard has no permanent-delete action, so nothing generates
    # `discard;` (which Proton documents as deleting "immediately and
    # permanently").
    TRASH = "trash"
    # Old name for TRASH, kept only as a compatibility alias: nothing in
    # src/ uses it (one test pins it), so use TRASH. Backups holding the old value
    # "delete" are read through LEGACY_ACTION_TYPES, not through this alias.
    DELETE = "trash"


# Proton's system folders as the wizard's "Move to" dropdown labels them,
# mapped to the action the scraper records. Names are the ones Proton's own
# wizard-to-Sieve generator (github.com/ProtonMail/sieve.js) writes in
# `fileinto`, which Proton's Sieve docs also use (`fileinto "trash";`).
# Trash and Archive have their own action types, generated as
# `fileinto "trash";` and `fileinto "archive";`.
SYSTEM_FOLDER_ACTIONS = {
    "Trash": {"type": "trash", "parameters": {}},
    "Archive": {"type": "archive", "parameters": {}},
    "Spam": {"type": "move_to", "parameters": {"folder": "spam"}},
    "Inbox - Default": {"type": "move_to", "parameters": {"folder": "inbox"}},
}


def escape_folder_segment(name: str) -> str:
    r"""Escape one folder or label name for use in a Proton `fileinto` path.

    Proton reads "/" in a fileinto target as the folder separator, so a "/"
    inside a single name is escaped with a backslash. Proton's Sieve docs:
    `"Work/Misc\\/Others"` in a script is the subfolder 'Misc/Others' of
    'Work'. The folder/label parameters stored on actions hold the
    unquoted path (`Work/Misc\/Others`); Sieve string quoting in the
    generator then doubles the backslash, giving exactly Proton's form.
    """
    return name.replace("/", "\\/")


def join_folder_path(segments: List[str]) -> str:
    """Join folder names (outermost first) into one escaped `fileinto` path."""
    return "/".join(escape_folder_segment(segment) for segment in segments)


# Action types older versions wrote to backups, and what they meant. The
# scraper recorded the Trash folder as "delete", and nothing else ever
# produced it, so it always meant Trash.
LEGACY_ACTION_TYPES = {"delete": "trash"}

# move_to folder targets older versions recorded for system folders: the
# dropdown label, written into `fileinto` verbatim.
LEGACY_FOLDER_TARGETS = {"Spam": "spam", "Inbox - Default": "inbox"}


def migrate_legacy_action(entry: dict) -> dict:
    """Rewrite an action dict from an older backup into its current form.

    Maps the old "delete" type to "trash" and the old system-folder targets
    ("Spam", "Inbox - Default") to Proton's Sieve names. Anything else is
    returned unchanged.
    """
    action_type = entry.get("type")
    if isinstance(action_type, str) and action_type in LEGACY_ACTION_TYPES:
        entry = dict(entry, type=LEGACY_ACTION_TYPES[action_type])
    params = entry.get("parameters")
    if entry.get("type") in ("move_to", ActionType.MOVE_TO) and isinstance(params, dict):
        folder = params.get("folder")
        if folder in LEGACY_FOLDER_TARGETS:
            entry = dict(entry, parameters=dict(params, folder=LEGACY_FOLDER_TARGETS[folder]))
    return entry


class FilterCondition(BaseModel):
    """One condition of a filter.

    A condition has either one `value` or, when the wizard shows several
    value chips (or consolidation merged several filters), a `values` list.
    A list is a Sieve key list: the condition matches if ANY value matches.
    It is only ever built from that structure, never inferred from text: a
    `value` is always one literal, even if it contains ", " or "|" (as old
    backups' joined chip values do), because reading separators into a
    literal would widen the rule.
    """
    type: ConditionType
    operator: Operator
    value: str = ""
    values: List[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def one_form_only(self):
        """Keep a single value in `value`; refuse a condition with both forms."""
        if self.values and self.value:
            raise ValueError("a condition has either value or values, not both")
        if len(self.values) == 1:
            self.value, self.values = self.values[0], []
        return self

    @model_serializer(mode='wrap')
    def omit_empty_values(self, handler):
        """Leave `values` out when unused, so single-value conditions
        serialize exactly as they did before the field existed."""
        data = handler(self)
        if isinstance(data, dict) and not data.get("values"):
            data.pop("values", None)
        return data

    @property
    def keys(self) -> List[str]:
        """The values this condition matches against (any one matching is enough)."""
        return list(self.values) if self.values else [self.value]

    @property
    def display_value(self) -> str:
        """The value(s) quoted for display: "a", or ["a", "b"] for a list."""
        return json.dumps(self.values) if self.values else json.dumps(self.value)


class FilterAction(BaseModel):
    type: ActionType
    parameters: dict = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def migrate_legacy(cls, data):
        """Read an action written by an older version in its current form."""
        if isinstance(data, dict):
            return migrate_legacy_action(data)
        return data


class LogicType(str, Enum):
    AND = "and"
    OR = "or"


class FilterStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    ARCHIVED = "archived"
    DEPRECATED = "deprecated"


class ScrapeEvidence(BaseModel):
    """Visible text captured from the filter wizard while scraping.

    The parsed conditions/actions are only as good as the scraper's
    understanding of the UI. This keeps what the UI actually showed, so a
    field the parser misses (or misreads) can still be recovered from the
    backup later instead of being lost when the UI filter is deleted.
    """
    conditions_text: str = ""  # Conditions step: visible text + form field states
    actions_text: str = ""     # Actions step: visible text + form field states
    sieve_text: str = ""       # Set instead of the above when Edit opened the Sieve editor


logger = logging.getLogger(__name__)

# The fields of a condition or action entry that must hold one of these enum
# values. Anything else has no defined meaning, so it is never guessed at.
_CONDITION_ENUM_FIELDS = (("type", "condition type", ConditionType), ("operator", "operator", Operator))
_ACTION_ENUM_FIELDS = (("type", "action type", ActionType),)


def unknown_value_problem(entry: dict, enum_fields) -> Optional[str]:
    """Describe the first missing or unknown enum value in a condition/action dict.

    Returns None when every field in enum_fields holds a valid value.
    """
    for key, label, enum_cls in enum_fields:
        value = entry.get(key)
        if value is None:
            return f"missing {label}"
        if isinstance(value, enum_cls):
            continue
        if value not in {member.value for member in enum_cls}:
            return f"unknown {label} {value!r}"
    return None


# Condition types that take no value: "has attachment" is a test of the
# message, not a comparison against text.
VALUELESS_CONDITION_TYPES = {"attachments"}


def empty_value_problem(entry) -> Optional[str]:
    """Describe a condition whose value is missing, empty or only whitespace.

    `entry` is a condition dict or a FilterCondition. An empty value is not
    "no restriction": `header :contains "Subject" ""` matches every message,
    so a rule built from it widens to all mail. Condition types in
    VALUELESS_CONDITION_TYPES are exempt. Returns None when the value is fine.
    """
    if isinstance(entry, dict):
        ctype, value, values = entry.get("type"), entry.get("value", ""), entry.get("values") or []
    else:
        ctype = getattr(entry, "type", None)
        value, values = getattr(entry, "value", ""), getattr(entry, "values", [])
    ctype = getattr(ctype, "value", ctype)
    if not isinstance(values, list):
        return f"values is not a list: {values!r}"
    if values and value:
        return "has both value and values"
    if ctype in VALUELESS_CONDITION_TYPES:
        return None
    for key in values or [value]:
        if not isinstance(key, str) or not key.strip():
            return f"empty value {key!r}"
    return None


def operator_mismatch_problem(entry) -> Optional[str]:
    """Describe a condition whose operator does not apply to its type.

    "has" is the attachment test and the attachment test takes only "has".
    Any other pairing (attachments contains X, sender has X) has no defined
    Sieve form, and guessing one could widen the rule.
    """
    if isinstance(entry, dict):
        ctype, operator = entry.get("type"), entry.get("operator")
    else:
        ctype, operator = getattr(entry, "type", None), getattr(entry, "operator", None)
    ctype = getattr(ctype, "value", ctype)
    operator = getattr(operator, "value", operator)
    if (ctype == ConditionType.ATTACHMENTS.value) != (operator == Operator.HAS.value):
        return f"operator {operator!r} does not apply to condition type {ctype!r}"
    return None


# Name prefix for filters rebuilt from the live Sieve section by
# consolidator.carry_forward. Shows up in the generated "# Source filters:"
# comments and in `snapshot view`. No brackets: Rich would parse them as markup.
CARRIED_PREFIX = "Carried forward"


def split_legacy_carried_values(data: dict) -> dict:
    """Turn an older carried-forward filter's "a|b|c" value into a values list.

    Before conditions had a values list, carry-forward stored several keys
    as one "|"-joined value and the generator split it again. Only a
    carried filter's value is read this way: carry-forward never produced a
    key containing "|", so there the "|" is always the join. Any other
    value is a literal.
    """
    if not str(data.get("name", "")).startswith(CARRIED_PREFIX):
        return data
    conditions = data.get("conditions")
    if not isinstance(conditions, list):
        return data
    migrated = []
    for cond in conditions:
        if isinstance(cond, dict) and not cond.get("values") and "|" in str(cond.get("value", "")):
            cond = dict(cond, value="", values=cond["value"].split("|"))
        migrated.append(cond)
    return dict(data, conditions=migrated)


class ProtonMailFilter(BaseModel):
    name: str
    enabled: bool = True
    status: FilterStatus = FilterStatus.ENABLED
    priority: int = 0
    logic: LogicType = LogicType.AND
    conditions: List[FilterCondition] = Field(default_factory=list)
    actions: List[FilterAction] = Field(default_factory=list)
    # Raw wizard text captured at scrape time. None for filters from backups
    # written before format 1.1, which had no evidence to recover from.
    raw: Optional[ScrapeEvidence] = None
    # Anything the scraper saw but could not parse. Non-empty means the
    # conditions/actions above may not be the whole filter.
    scrape_issues: List[str] = Field(default_factory=list)
    # True when Edit opened the Sieve code editor instead of the wizard. Such
    # a filter is a script (raw.sieve_text), not conditions and actions, so
    # its empty conditions/actions say nothing about what it does. It is
    # never consolidated, never counted as covered, and never deleted.
    is_sieve: bool = False

    @property
    def is_complete(self) -> bool:
        """True if the scraper reported no unparsed or unreadable fields."""
        return not self.scrape_issues

    @model_validator(mode='before')
    @classmethod
    def quarantine_unknown_values(cls, data):
        """Flag, rather than guess or reject, entries with no defined meaning.

        A condition or action whose type/operator is missing or not one the
        model knows (a hand-edited backup, a parser that let one through),
        or a condition whose value is empty or whitespace (see
        empty_value_problem), is dropped and recorded in scrape_issues,
        entry included, so the filter is incomplete: consolidate leaves it
        out and cleanup will not delete it. Guessing a value could widen a rule; rejecting would stop a
        whole backup loading over one filter. An unknown logic value is
        treated the same way; a missing one stays AND, as for backups made
        before the field existed.
        """
        if not isinstance(data, dict):
            return data
        data = split_legacy_carried_values(dict(data))
        issues = []
        for key, kind, enum_fields in (
            ("conditions", "condition", _CONDITION_ENUM_FIELDS),
            ("actions", "action", _ACTION_ENUM_FIELDS),
        ):
            entries = data.get(key)
            if not isinstance(entries, list):
                continue
            kept = []
            for index, entry in enumerate(entries, 1):
                if key == "actions" and isinstance(entry, dict):
                    entry = migrate_legacy_action(entry)
                problem = unknown_value_problem(entry, enum_fields) if isinstance(entry, dict) else None
                if problem is None and key == "conditions":
                    problem = operator_mismatch_problem(entry) or empty_value_problem(entry)
                if problem:
                    shown = entry.model_dump(mode="json") if isinstance(entry, BaseModel) else entry
                    issues.append(f"{kind} {index}: {problem}; dropped {json.dumps(shown, default=str)}")
                else:
                    kept.append(entry)
            data[key] = kept
        logic = data.get("logic")
        if logic is not None and not isinstance(logic, LogicType) and logic not in {m.value for m in LogicType}:
            issues.append(f"unknown logic {logic!r}; read as 'and'")
            data["logic"] = LogicType.AND
        if issues:
            for issue in issues:
                logger.warning("Filter '%s' incomplete: %s", data.get("name", "?"), issue)
            data["scrape_issues"] = list(data.get("scrape_issues") or []) + issues
        return data

    @model_validator(mode='before')
    @classmethod
    def derive_status_from_enabled(cls, data):
        """Backward compat: if status absent, derive from enabled bool."""
        if isinstance(data, dict):
            if 'status' not in data:
                enabled = data.get('enabled', True)
                data['status'] = FilterStatus.ENABLED if enabled else FilterStatus.DISABLED
            else:
                # Keep enabled in sync with status
                status = data['status']
                if isinstance(status, str):
                    status = FilterStatus(status)
                data['enabled'] = status == FilterStatus.ENABLED
        return data

    @model_validator(mode='before')
    @classmethod
    def derive_is_sieve_from_evidence(cls, data):
        """Backward compat: backups written before is_sieve existed mark a
        Sieve filter only by its captured script, so derive the flag from it."""
        if isinstance(data, dict) and 'is_sieve' not in data:
            raw = data.get('raw')
            if isinstance(raw, dict):
                sieve_text = raw.get('sieve_text', '')
            else:
                sieve_text = getattr(raw, 'sieve_text', '')
            if sieve_text:
                data['is_sieve'] = True
        return data

    @property
    def content_hash(self) -> str:
        """Content-addressable hash of filter identity (name + logic + conditions + actions).

        Excludes enabled/status/priority since those don't define the filter's purpose.
        A Sieve filter's identity is its script, so that is hashed too; otherwise
        every Sieve filter with the same name would share one hash.
        """
        parts = [
            f"name={self.name}",
            f"logic={self.logic.value}",
        ]
        for c in self.conditions:
            if c.values:
                # Distinct prefix, so a list never hashes like some literal
                parts.append(f"conds:{c.type.value}|{c.operator.value}|{json.dumps(c.values)}")
            else:
                parts.append(f"cond:{c.type.value}|{c.operator.value}|{c.value}")
        for a in self.actions:
            params = ",".join(f"{k}={v}" for k, v in sorted(a.parameters.items()))
            parts.append(f"act:{a.type.value}|{params}")
        if self.is_sieve:
            parts.append(f"sieve={self.raw.sieve_text if self.raw else ''}")
        raw = "\n".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class ConditionGroup(BaseModel):
    """A group of conditions from a single original filter, preserving its logic.

    When filters are consolidated, each original filter's conditions become
    a ConditionGroup. Groups are OR'd together (any group matching triggers
    the action), while conditions within a group keep their original logic.
    """
    logic: LogicType = LogicType.AND
    conditions: List[FilterCondition] = Field(default_factory=list)


class ConsolidatedFilter(BaseModel):
    """Optimized filter with source tracking.

    condition_groups are OR'd together: if any group matches, the actions fire.
    Each group preserves the original filter's internal logic (AND/OR).
    """
    name: str
    condition_groups: List[ConditionGroup] = Field(default_factory=list)
    actions: List[FilterAction] = Field(default_factory=list)
    source_filters: List[str] = Field(default_factory=list)  # original filter names
    filter_count: int = 0  # how many filters were merged
