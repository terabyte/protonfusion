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

One deliberate exception: a live action older versions generated for a
system folder (see sieve_rules.LEGACY_ACTION_FIXES, chiefly `discard;` for
"Move to Trash") is carried forward in its corrected form, and verified
against that. The live section is ProtonFusion's own output, and the
wizard choice behind it never meant a permanent delete.
"""

from __future__ import annotations

from collections import defaultdict
from typing import AbstractSet, Dict, Iterable, List, Optional, Set, Tuple

from src.generator.sieve_generator import (
    SieveGenerationError, SieveGenerator, escape_match_literal, unescape_match_literal,
)
from src.generator.sieve_generator import TRASH_FOLDER
from src.generator.sieve_rules import (
    Atom, Fact, _tokenize, correct_legacy_actions, describe_atom, script_facts,
)
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
    if operator == Operator.MATCHES:
        operator, value = _matches_operator(value)
    # The generator splits values on "|" and ", ", so such values cannot round-trip
    if "|" in value or ", " in value or not value:
        return None
    return FilterCondition(type=ctype, operator=operator, value=value)


def _matches_operator(pattern: str) -> Tuple[Operator, str]:
    """Classify a :matches pattern as begins-with, ends-with, or a raw pattern.

    SieveGenerator writes begins-with as ``escaped*`` and ends-with as
    ``*escaped``, so a pattern of exactly that shape maps back to the friendlier
    operator and its literal value. Anything else stays a MATCHES pattern. The
    re-escape check keeps the mapping exact, so regeneration reproduces the
    same pattern byte for byte.
    """
    for operator, literal_part, wrap in (
        (Operator.STARTS_WITH, pattern[:-1], lambda v: v + "*"),
        (Operator.ENDS_WITH, pattern[1:], lambda v: "*" + v),
    ):
        if len(pattern) < 2 or wrap(literal_part) != pattern:
            continue
        literal = unescape_match_literal(literal_part)
        if literal and wrap(escape_match_literal(literal)) == pattern:
            return operator, literal
    return Operator.MATCHES, pattern


def _action_text_to_action(text: str, label_names: AbstractSet[str] = frozenset()) -> Optional[FilterAction]:
    """Map one canonical action statement (e.g. 'fileinto "X";') to a FilterAction.

    Folders and labels both become `fileinto`, so the Sieve alone cannot tell
    them apart. A target in `label_names` (known only as a label) becomes
    LABEL; anything else becomes MOVE_TO. Either generates the same Sieve,
    but the type shows in `snapshot view` and sets the rule's ordering.
    """
    tokens = _tokenize(text)
    words = [(t.kind, t.value) for t in tokens]
    if len(words) == 3 and words[0] == ("ident", "fileinto") and words[1][0] == "string" and words[2][0] == ";":
        folder = words[1][1]
        if folder == "Archive":
            return FilterAction(type=ActionType.ARCHIVE)
        if folder == TRASH_FOLDER:
            return FilterAction(type=ActionType.TRASH)
        if folder in label_names:
            return FilterAction(type=ActionType.LABEL, parameters={"label": folder})
        return FilterAction(type=ActionType.MOVE_TO, parameters={"folder": folder})
    if len(words) == 3 and words[0] == ("ident", "addflag") and words[2][0] == ";":
        if words[1] == ("string", "\\Seen"):
            return FilterAction(type=ActionType.MARK_READ)
        if words[1] == ("string", "\\Flagged"):
            return FilterAction(type=ActionType.STAR)
    return None


def _actions_for(
    action_texts: Iterable[str], label_names: AbstractSet[str] = frozenset(),
) -> Optional[List[FilterAction]]:
    """Convert an action set; None if any action is not representable."""
    texts = sorted(action_texts)
    if texts == ["keep;"]:
        # The generator writes `keep;` for a filter with no actions
        return []
    actions = []
    for text in texts:
        action = _action_text_to_action(text, label_names)
        if action is None:
            return None
        actions.append(action)
    return actions


def _describe_actions(action_texts: Iterable[str]) -> str:
    return " ".join(sorted(action_texts)) or "keep;"


def filter_facts(f: ProtonMailFilter) -> Set[Fact]:
    """The Sieve facts one filter contributes when generated on its own.

    A filter the generator refuses (no conditions, for example) contributes
    a single placeholder fact that no Sieve script can contain. So it is
    never "covered" by a live section (cleanup keeps it) and suppresses no
    real rule.
    """
    cf = ConsolidatedFilter(
        name=f.name,
        condition_groups=[ConditionGroup(logic=f.logic, conditions=f.conditions)],
        actions=f.actions,
        source_filters=[f.name],
        filter_count=1,
    )
    try:
        return script_facts(SieveGenerator().generate([cf]))
    except SieveGenerationError as e:
        return {Fact(frozenset({("ungeneratable", f"cannot generate: {e}")}), frozenset())}


def label_targets(filters: Iterable[ProtonMailFilter]) -> Set[str]:
    """Names the given filters use only as labels, never as a folder.

    A name used both ways is left out, so a carried rule for it falls back
    to MOVE_TO rather than guessing.
    """
    labels: Set[str] = set()
    folders: Set[str] = set()
    for f in filters:
        for a in f.actions:
            if a.type == ActionType.LABEL and a.parameters.get("label"):
                labels.add(a.parameters["label"])
            elif a.type == ActionType.MOVE_TO and a.parameters.get("folder"):
                folders.add(a.parameters["folder"])
    return labels - folders


def facts_to_filters(
    facts: Iterable[Fact], label: str = "", label_names: AbstractSet[str] = frozenset(),
) -> Tuple[List[ProtonMailFilter], List[Fact]]:
    """Rebuild ARCHIVED ProtonMailFilters that reproduce exactly the given facts.

    `label` (typically the snapshot name) is embedded in each filter name so
    filters carried forward in different runs never share a name.

    `label_names` are `fileinto` targets known to be labels (see
    label_targets); their actions come back as LABEL instead of MOVE_TO.

    Single-atom facts sharing an action set, condition type and operator are
    packed into one filter with a pipe-joined value (which the generator
    expands to a Sieve key list). Multi-atom facts (from allof tests) each
    become their own AND filter.

    A fact using an action older versions generated for a system folder
    (`discard;` for Trash) is converted in its corrected form; see the
    module docstring. Unconvertible facts are always returned as given.

    Returns (filters, unconvertible_facts).
    """
    # corrected fact -> the given fact(s) it came from
    originals: Dict[Fact, List[Fact]] = defaultdict(list)
    by_actions: Dict[frozenset, List[Fact]] = defaultdict(list)
    for given in facts:
        fact = correct_legacy_actions(given)
        if fact not in originals:
            by_actions[fact.actions].append(fact)
        originals[fact].append(given)

    filters: List[ProtonMailFilter] = []
    unconvertible: List[Fact] = []

    for action_texts, group in sorted(by_actions.items(), key=lambda kv: _describe_actions(kv[0])):
        actions = _actions_for(action_texts, label_names)
        if actions is None:
            unconvertible.extend(g for fact in group for g in originals[fact])
            continue
        action_desc = _describe_actions(action_texts)
        prefix = f"{CARRIED_PREFIX} ({label}):" if label else f"{CARRIED_PREFIX}:"

        # (condition type, operator) -> [(value, fact)] for single-atom facts
        packed: Dict[Tuple[ConditionType, Operator], List[Tuple[str, Fact]]] = defaultdict(list)
        candidates: List[Tuple[ProtonMailFilter, Set[Fact]]] = []

        for fact in sorted(group, key=Fact.describe):
            conditions = [_atom_to_condition(a) for a in sorted(fact.conditions)]
            if not conditions or any(c is None for c in conditions):
                unconvertible.extend(originals[fact])
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
                unconvertible.extend(
                    g for fact in sorted(expected, key=Fact.describe) for g in originals[fact]
                )

    return filters, unconvertible
