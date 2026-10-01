import hashlib
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


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
