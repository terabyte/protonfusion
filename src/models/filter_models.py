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


class ConsolidatedFilter(BaseModel):
    """Optimized filter with source tracking."""
    name: str
    logic: LogicType = LogicType.OR
    conditions: List[FilterCondition] = Field(default_factory=list)
    actions: List[FilterAction] = Field(default_factory=list)
    source_filters: List[str] = Field(default_factory=list)  # original filter names
    filter_count: int = 0  # how many filters were merged
