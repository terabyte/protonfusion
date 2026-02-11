import hashlib
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


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
    DELETE = "delete"


class FilterCondition(BaseModel):
    type: ConditionType
    operator: Operator
    value: str = ""


class FilterAction(BaseModel):
    type: ActionType
    parameters: dict = Field(default_factory=dict)


class LogicType(str, Enum):
    AND = "and"
    OR = "or"


class ProtonMailFilter(BaseModel):
    name: str
    enabled: bool = True
    priority: int = 0
    logic: LogicType = LogicType.AND
    conditions: List[FilterCondition] = Field(default_factory=list)
    actions: List[FilterAction] = Field(default_factory=list)

    @property
    def content_hash(self) -> str:
        """Content-addressable hash of filter identity (name + logic + conditions + actions).

        Excludes enabled/priority since those don't define the filter's purpose.
        """
        parts = [
            f"name={self.name}",
            f"logic={self.logic.value}",
        ]
        for c in self.conditions:
            parts.append(f"cond:{c.type.value}|{c.operator.value}|{c.value}")
        for a in self.actions:
            params = ",".join(f"{k}={v}" for k, v in sorted(a.parameters.items()))
            parts.append(f"act:{a.type.value}|{params}")
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
