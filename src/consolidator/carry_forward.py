"""Carry rules forward from the live ProtonFusion Sieve section.

After `cleanup` deletes the original UI filters, some rules may exist only in
the live ProtonFusion section (for example, rules consolidated before the
archive system existed, or an archive that was lost). `consolidate
--keep-live-rules` uses this module to turn every live rule that the new
consolidation would drop back into ProtonMailFilter objects, which are then
stored in archive.json as ARCHIVED filters. From there they flow through the
normal pipeline and are carried forward by the archive on every later backup,
so the live section never again has to be the only copy.

Conversion is the inverse of SieveGenerator for the constructs it emits, and
every converted filter is verified by regenerating it and checking that it
yields exactly the facts it was built from. Anything that cannot be
reproduced exactly is returned as unconvertible rather than approximated.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.generator.sieve_generator import SieveGenerator
from src.generator.sieve_rules import Atom, Fact, _tokenize, describe_atom, script_facts
from src.models.filter_models import (
    ActionType, ConditionGroup, ConditionType, ConsolidatedFilter, FilterAction,
    FilterCondition, FilterStatus, LogicType, Operator, ProtonMailFilter,
)

# Name prefix for filters synthesized from live Sieve rules. Shows up in the
# generated "# Source filters:" comments and in `snapshot view`.
CARRIED_PREFIX = "Carried forward"  # no brackets: Rich would parse them as markup


def is_carried(f: ProtonMailFilter) -> bool:
    """True for a filter rebuilt from the live Sieve section by facts_to_filters.

    Such a filter has no scrape evidence (raw is None) because it was never
    scraped; it is verified by regenerating it instead.
    """
    return f.name.startswith(CARRIED_PREFIX)

_MATCH_TO_OPERATOR = {
    ":is": Operator.IS,
    ":contains": Operator.CONTAINS,
    ":matches": Operator.MATCHES,
}

# (test name, sorted header tuple) -> condition type, mirroring
# SieveGenerator._condition_to_sieve.
_HEADERS_TO_TYPE = {
    ("address", ("from",)): ConditionType.SENDER,
    ("address", ("bcc", "cc", "to")): ConditionType.RECIPIENT,
    ("header", ("subject",)): ConditionType.SUBJECT,
    ("header", ("x-custom",)): ConditionType.HEADER,
}


def _atom_to_condition(atom: Atom) -> Optional[FilterCondition]:
    """Map one test atom back to a FilterCondition, or None if not representable."""
    if atom == ("true",):
        # SieveGenerator emits a bare `true` test for attachment conditions
        return FilterCondition(type=ConditionType.ATTACHMENTS, operator=Operator.HAS, value="")
    if len(atom) != 6:
        return None
    name, match, addrpart, comparator, headers, value = atom
    ctype = _HEADERS_TO_TYPE.get((name, headers))
    operator = _MATCH_TO_OPERATOR.get(match)
    if ctype is None or operator is None or addrpart != ":all" or comparator != "i;ascii-casemap":
        return None
    # The generator splits values on "|" and ", ", so such values cannot round-trip
    if "|" in value or ", " in value or not value:
        return None
    return FilterCondition(type=ctype, operator=operator, value=value)


def _action_text_to_action(text: str) -> Optional[FilterAction]:
    """Map one canonical action statement (e.g. 'fileinto "X";') to a FilterAction."""
    tokens = _tokenize(text)
    words = [(t.kind, t.value) for t in tokens]
    if words == [("ident", "discard"), (";", ";")]:
        return FilterAction(type=ActionType.DELETE)
    if len(words) == 3 and words[0] == ("ident", "fileinto") and words[1][0] == "string" and words[2][0] == ";":
        folder = words[1][1]
        if folder == "Archive":
            return FilterAction(type=ActionType.ARCHIVE)
        return FilterAction(type=ActionType.MOVE_TO, parameters={"folder": folder})
    if len(words) == 3 and words[0] == ("ident", "addflag") and words[2][0] == ";":
        if words[1] == ("string", "\\Seen"):
            return FilterAction(type=ActionType.MARK_READ)
        if words[1] == ("string", "\\Flagged"):
            return FilterAction(type=ActionType.STAR)
    return None


def _actions_for(action_texts: Iterable[str]) -> Optional[List[FilterAction]]:
    """Convert an action set; None if any action is not representable."""
    texts = sorted(action_texts)
    if texts == ["keep;"]:
        # The generator writes `keep;` for a filter with no actions
        return []
    actions = []
    for text in texts:
        action = _action_text_to_action(text)
        if action is None:
            return None
        actions.append(action)
    return actions


def _describe_actions(action_texts: Iterable[str]) -> str:
    return " ".join(sorted(action_texts)) or "keep;"


def filter_facts(f: ProtonMailFilter) -> Set[Fact]:
    """The Sieve facts one filter contributes when generated on its own."""
    cf = ConsolidatedFilter(
        name=f.name,
        condition_groups=[ConditionGroup(logic=f.logic, conditions=f.conditions)],
        actions=f.actions,
        source_filters=[f.name],
        filter_count=1,
    )
    return script_facts(SieveGenerator().generate([cf]))


def facts_to_filters(
    facts: Iterable[Fact], label: str = "",
) -> Tuple[List[ProtonMailFilter], List[Fact]]:
    """Rebuild ARCHIVED ProtonMailFilters that reproduce exactly the given facts.

    `label` (typically the snapshot name) is embedded in each filter name so
    filters carried forward in different runs never share a name.

    Single-atom facts sharing an action set, condition type and operator are
    packed into one filter with a pipe-joined value (which the generator
    expands to a Sieve key list). Multi-atom facts (from allof tests) each
    become their own AND filter.

    Returns (filters, unconvertible_facts).
    """
    by_actions: Dict[frozenset, List[Fact]] = defaultdict(list)
    for fact in facts:
        by_actions[fact.actions].append(fact)

    filters: List[ProtonMailFilter] = []
    unconvertible: List[Fact] = []

    for action_texts, group in sorted(by_actions.items(), key=lambda kv: _describe_actions(kv[0])):
        actions = _actions_for(action_texts)
        if actions is None:
            unconvertible.extend(group)
            continue
        action_desc = _describe_actions(action_texts)
        prefix = f"{CARRIED_PREFIX} ({label}):" if label else f"{CARRIED_PREFIX}:"

        # (condition type, operator) -> [(value, fact)] for single-atom facts
        packed: Dict[Tuple[ConditionType, Operator], List[Tuple[str, Fact]]] = defaultdict(list)
        candidates: List[Tuple[ProtonMailFilter, Set[Fact]]] = []

        for fact in sorted(group, key=Fact.describe):
            conditions = [_atom_to_condition(a) for a in sorted(fact.conditions)]
            if not conditions or any(c is None for c in conditions):
                unconvertible.append(fact)
                continue
            if len(conditions) == 1 and conditions[0].type != ConditionType.ATTACHMENTS:
                packed[(conditions[0].type, conditions[0].operator)].append((conditions[0].value, fact))
                continue
            name_conds = " AND ".join(describe_atom(a) for a in sorted(fact.conditions))
            candidates.append((
                ProtonMailFilter(
                    name=f"{prefix} {name_conds} -> {action_desc}",
                    logic=LogicType.AND,
                    conditions=conditions,
                    actions=actions,
                ),
                {fact},
            ))

        for (ctype, operator), entries in sorted(packed.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)):
            values = sorted({v for v, _ in entries})
            candidates.append((
                ProtonMailFilter(
                    name=f"{prefix} {ctype.value} {operator.value} -> {action_desc}",
                    logic=LogicType.AND,
                    conditions=[FilterCondition(type=ctype, operator=operator, value="|".join(values))],
                    actions=actions,
                ),
                {fact for _, fact in entries},
            ))

        for candidate, expected in candidates:
            # Verify: regenerating the filter must give back exactly these facts
            if filter_facts(candidate) == expected:
                filters.append(candidate.model_copy(update={
                    "status": FilterStatus.ARCHIVED, "enabled": False,
                }))
            else:
                unconvertible.extend(sorted(expected, key=Fact.describe))

    return filters, unconvertible
