"""Structural parsing and comparison of the ProtonFusion Sieve section.

Why this exists: after ``cleanup`` deletes the original UI filters, the live
ProtonFusion section of the Sieve script is the only remaining copy of their
rules. A later backup -> consolidate -> sync regenerates the section from
whatever UI filters are left, so without a check it would silently delete
every rule whose UI filter is gone. ``compare_sections`` detects that.

The comparison is structural rather than textual. Each rule is reduced to a
set of *facts*, where a fact is one (condition clause, action set) pair:

* The rule's test is expanded into disjunctive normal form: a set of clauses,
  each clause a set of atoms that must all match. ``anyof`` unions clauses,
  ``allof`` takes the cross product, and a test with a list of keys
  (``address :is "From" ["a", "b"]``) is one clause per key, because Sieve
  matches a key list if *any* key matches.
* The rule's actions (``fileinto "X"``, ``addflag "\\\\Seen"``, ``discard``,
  ``stop`` ...) become an unordered set.

So ``if address :is "From" ["a", "b"] { discard; }`` yields two facts:
(From is a -> discard) and (From is b -> discard). A new section "drops" a
fact when that exact pair appears nowhere in it. Regrouping, reordering, or
merging senders into bigger arrays does not count as a drop; removing a
sender, or changing what happens to its mail, does.

Limits (see docs/sieve-reference.md, "Rule Preservation"):

* Rule order and ``stop`` interactions *between* rules are not compared. Two
  sections with the same facts in a different order can behave differently
  if a rule stops processing.
* Anything the parser does not model (``not``, ``size``, ``exists``,
  ``elsif``/``else`` chains, nested ``if``, relational match types) is kept as
  an opaque fact keyed on its canonical text. It only counts as preserved if
  the new section contains the identical construct, so the check fails
  closed rather than open.
* Values are compared case-insensitively only where Sieve does (the default
  ``i;ascii-casemap`` comparator for tests); action arguments such as folder
  names are compared exactly.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple, Union

from src.generator.sieve_generator import SECTION_BEGIN, SECTION_END, escape_match_literal

# A single test atom, e.g. ("address", ":is", ":all", "", ("from",), "a@x.com").
# Opaque constructs are ("opaque", <canonical text>).
Atom = Tuple[str, ...]
Clause = FrozenSet[Atom]

# Tests that compare header/address values against a key list and that we model.
_KEYED_TESTS = {"address", "header", "envelope"}
_MATCH_TYPES = {":is", ":contains", ":matches", ":regex"}
_ADDRESS_PARTS = {":all", ":localpart", ":domain"}
_DEFAULT_COMPARATOR = "i;ascii-casemap"

# Upper bound on clauses produced by expanding one allof(); beyond this the
# test is treated as opaque instead of exploding memory.
_MAX_CLAUSES_PER_TEST = 50_000

# Upper bound on wildcard-less :matches atoms in one fact that
# _legacy_wildcard_variants will expand (3 ** n variants).
_MAX_LEGACY_ATOMS = 6


class SieveParseError(ValueError):
    """Raised when a Sieve section cannot be tokenized or parsed at all."""


# --- Tokenizer ---------------------------------------------------------------

@dataclass(frozen=True)
class _Token:
    kind: str  # "ident", "tag", "string", "number", or the punctuation char
    value: str


def _tokenize(text: str) -> List[_Token]:
    """Split Sieve source into tokens, dropping comments and whitespace."""
    tokens: List[_Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "#":
            newline = text.find("\n", i)
            i = n if newline == -1 else newline + 1
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close == -1:
                raise SieveParseError("unterminated /* comment")
            i = close + 2
        elif ch == '"':
            i += 1
            chars = []
            while True:
                if i >= n:
                    raise SieveParseError("unterminated string")
                c = text[i]
                if c == "\\" and i + 1 < n:
                    # RFC 5228: backslash escapes the next character
                    chars.append(text[i + 1])
                    i += 2
                elif c == '"':
                    i += 1
                    break
                else:
                    chars.append(c)
                    i += 1
            tokens.append(_Token("string", "".join(chars)))
        elif ch == ":":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            if j == i + 1:
                raise SieveParseError(f"bare ':' at offset {i}")
            tokens.append(_Token("tag", text[i:j].lower()))
            i = j
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word.lower() == "text" and j < n and text[j] == ":":
                raise SieveParseError("multi-line text: literals are not supported")
            tokens.append(_Token("ident", word.lower()))
            i = j
        elif ch.isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] in "KMGkmg":
                j += 1
            tokens.append(_Token("number", text[i:j].upper()))
            i = j
        elif ch in "[](){},;":
            tokens.append(_Token(ch, ch))
            i += 1
        else:
            raise SieveParseError(f"unexpected character {ch!r} at offset {i}")
    return tokens


# --- Parser ------------------------------------------------------------------

Argument = Union[_Token, Tuple[str, ...]]  # tag/number/string token, or a string list


@dataclass
class _Test:
    name: str
    args: List[Argument] = field(default_factory=list)
    subtests: List["_Test"] = field(default_factory=list)


@dataclass
class _Command:
    name: str
    args: List[Argument] = field(default_factory=list)
    tests: List[_Test] = field(default_factory=list)
    block: Optional[List["_Command"]] = None


class _Parser:
    """Recursive-descent parser for the RFC 5228 command grammar."""

    def __init__(self, tokens: List[_Token]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> Optional[_Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> _Token:
        tok = self._peek()
        if tok is None:
            raise SieveParseError("unexpected end of script")
        self.pos += 1
        return tok

    def _expect(self, kind: str) -> _Token:
        tok = self._next()
        if tok.kind != kind:
            raise SieveParseError(f"expected {kind!r}, got {tok.value!r}")
        return tok

    def parse_commands(self, in_block: bool = False) -> List[_Command]:
        commands = []
        while True:
            tok = self._peek()
            if tok is None:
                if in_block:
                    raise SieveParseError("unterminated block")
                return commands
            if tok.kind == "}":
                if not in_block:
                    raise SieveParseError("unexpected '}'")
                return commands
            commands.append(self._parse_command())

    def _parse_arguments(self) -> List[Argument]:
        args: List[Argument] = []
        while True:
            tok = self._peek()
            if tok is None:
                return args
            if tok.kind in ("tag", "number", "string"):
                args.append(self._next())
            elif tok.kind == "[":
                args.append(self._parse_string_list())
            else:
                return args

    def _parse_string_list(self) -> Tuple[str, ...]:
        self._expect("[")
        values = [self._expect("string").value]
        while self._peek() is not None and self._peek().kind == ",":
            self._next()
            values.append(self._expect("string").value)
        self._expect("]")
        return tuple(values)

    def _parse_test(self) -> _Test:
        name = self._expect("ident").value
        test = _Test(name=name, args=self._parse_arguments())
        tok = self._peek()
        if tok is not None and tok.kind == "(":
            self._next()
            test.subtests.append(self._parse_test())
            while self._peek() is not None and self._peek().kind == ",":
                self._next()
                test.subtests.append(self._parse_test())
            self._expect(")")
        elif tok is not None and tok.kind == "ident" and name == "not":
            # `not` is the only core test taking a single bare test argument
            test.subtests.append(self._parse_test())
        return test

    def _parse_command(self) -> _Command:
        name = self._expect("ident").value
        cmd = _Command(name=name, args=self._parse_arguments())
        tok = self._peek()
        if tok is not None and tok.kind == "ident":
            cmd.tests.append(self._parse_test())
        elif tok is not None and tok.kind == "(":
            self._next()
            cmd.tests.append(self._parse_test())
            while self._peek() is not None and self._peek().kind == ",":
                self._next()
                cmd.tests.append(self._parse_test())
            self._expect(")")
        tok = self._next()
        if tok.kind == ";":
            if name in ("if", "elsif", "else"):
                raise SieveParseError(f"{name!r} without a block")
            return cmd
        if tok.kind == "{":
            cmd.block = self.parse_commands(in_block=True)
            self._expect("}")
            return cmd
        raise SieveParseError(f"expected ';' or '{{' after {name!r}, got {tok.value!r}")


# --- Canonical text (for display and for opaque facts) -----------------------

def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _format_arg(arg: Argument) -> str:
    if isinstance(arg, tuple):
        return "[" + ", ".join(_quote(v) for v in arg) + "]"
    if arg.kind == "string":
        return _quote(arg.value)
    return arg.value


def _format_test(test: _Test) -> str:
    parts = [test.name] + [_format_arg(a) for a in test.args]
    text = " ".join(parts)
    if test.subtests:
        text += " (" + ", ".join(_format_test(t) for t in test.subtests) + ")"
    return text


def _format_command(cmd: _Command) -> str:
    parts = [cmd.name] + [_format_arg(a) for a in cmd.args]
    if cmd.tests:
        parts.append(", ".join(_format_test(t) for t in cmd.tests))
    text = " ".join(parts)
    if cmd.block is None:
        return text + ";"
    return text + " { " + " ".join(_format_command(c) for c in cmd.block) + " }"


# --- Facts -------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    """One (condition clause -> action set) pair extracted from a rule.

    ``conditions`` is a set of atoms that must all match. ``actions`` is the
    unordered set of canonical action statements the rule runs.
    """
    conditions: Clause
    actions: FrozenSet[str]

    def describe(self) -> str:
        """Human-readable one-liner, e.g. 'address From :is "a@x" -> discard;'."""
        cond = " AND ".join(sorted(describe_atom(a) for a in self.conditions)) or "(always)"
        acts = " ".join(sorted(self.actions)) or "(no actions)"
        return f"{cond}  ->  {acts}"


_ALWAYS: Clause = frozenset({("true",)})


def describe_atom(atom: Atom) -> str:
    """Render one atom the way it would read in Sieve."""
    if atom[0] == "true":
        return "true"
    if atom[0] in ("opaque", "rule"):
        return atom[1]
    name, match, addrpart, comparator, headers, value = atom
    header_text = ",".join(headers)
    extras = [match]
    if name != "header" and addrpart != ":all":
        extras.append(addrpart)
    if comparator != _DEFAULT_COMPARATOR:
        extras.append(f':comparator "{comparator}"')
    return f"{name} {header_text} {' '.join(extras)} {_quote(value)}"


def _keyed_test_clauses(test: _Test) -> Optional[Set[Clause]]:
    """Expand an address/header/envelope test into one clause per key.

    Returns None if the test uses arguments we don't model, so the caller can
    fall back to an opaque atom.
    """
    match = ":is"
    addrpart = ":all"
    comparator = _DEFAULT_COMPARATOR
    string_args: List[Tuple[str, ...]] = []
    args = list(test.args)
    i = 0
    while i < len(args):
        arg = args[i]
        if isinstance(arg, tuple):
            string_args.append(arg)
        elif arg.kind == "string":
            string_args.append((arg.value,))
        elif arg.kind == "tag" and arg.value in _MATCH_TYPES:
            match = arg.value
        elif arg.kind == "tag" and arg.value in _ADDRESS_PARTS and test.name != "header":
            addrpart = arg.value
        elif arg.kind == "tag" and arg.value == ":comparator" and i + 1 < len(args):
            nxt = args[i + 1]
            if isinstance(nxt, tuple) or nxt.kind != "string":
                return None
            comparator = nxt.value.lower()
            i += 1
        else:
            return None
        i += 1
    if len(string_args) != 2 or test.subtests:
        return None

    headers = tuple(sorted(h.lower() for h in string_args[0]))
    keys = string_args[1]
    if comparator == _DEFAULT_COMPARATOR and match != ":regex":
        keys = tuple(k.lower() for k in keys)
    return {
        frozenset({(test.name, match, addrpart, comparator, headers, key)})
        for key in keys
    }


def _test_clauses(test: _Test) -> Set[Clause]:
    """Expand a test into disjunctive normal form: a set of AND-clauses."""
    if test.name == "anyof" and not test.args:
        clauses: Set[Clause] = set()
        for sub in test.subtests:
            clauses |= _test_clauses(sub)
        return clauses

    if test.name == "allof" and not test.args:
        per_sub = [_test_clauses(sub) for sub in test.subtests]
        size = 1
        for clauses in per_sub:
            size *= max(len(clauses), 1)
        if size <= _MAX_CLAUSES_PER_TEST:
            return {
                frozenset().union(*combo)
                for combo in itertools.product(*per_sub)
            }

    elif test.name == "true" and not test.args and not test.subtests:
        return {_ALWAYS}

    elif test.name in _KEYED_TESTS:
        keyed = _keyed_test_clauses(test)
        if keyed is not None:
            return keyed

    return {frozenset({("opaque", _format_test(test))})}


@dataclass
class ParsedRule:
    """One top-level rule of a section and the facts it contributes."""
    text: str
    facts: Set[Fact]
    opaque: bool = False


def _is_simple_action(cmd: _Command) -> bool:
    return cmd.block is None and not cmd.tests and cmd.name not in ("if", "elsif", "else", "require")


def parse_rules(section_text: str) -> List[ParsedRule]:
    """Parse the body of a ProtonFusion section into rules and their facts.

    Raises SieveParseError if the text is not syntactically valid Sieve.
    """
    commands = _Parser(_tokenize(section_text)).parse_commands()
    rules: List[ParsedRule] = []
    i = 0
    while i < len(commands):
        cmd = commands[i]
        if cmd.name == "require":
            i += 1
            continue

        if cmd.name == "if":
            chain = [cmd]
            while i + len(chain) < len(commands) and commands[i + len(chain)].name in ("elsif", "else"):
                chain.append(commands[i + len(chain)])
            text = " ".join(_format_command(c) for c in chain)
            simple = (
                len(chain) == 1
                and len(cmd.tests) == 1
                and not cmd.args
                and cmd.block is not None
                and all(_is_simple_action(a) for a in cmd.block)
            )
            if simple:
                actions = frozenset(_format_command(a) for a in cmd.block)
                facts = {Fact(clause, actions) for clause in _test_clauses(cmd.tests[0])}
                rules.append(ParsedRule(text=text, facts=facts))
            else:
                # elsif/else chains and nested ifs: kept whole, compared by text
                opaque = Fact(frozenset({("rule", text)}), frozenset())
                rules.append(ParsedRule(text=text, facts={opaque}, opaque=True))
            i += len(chain)
            continue

        text = _format_command(cmd)
        if _is_simple_action(cmd):
            # Unconditional action at section top level
            rules.append(ParsedRule(text=text, facts={Fact(_ALWAYS, frozenset({text}))}))
        else:
            opaque = Fact(frozenset({("rule", text)}), frozenset())
            rules.append(ParsedRule(text=text, facts={opaque}, opaque=True))
        i += 1
    return rules


def extract_section(script: str) -> Optional[str]:
    """Return the text between the ProtonFusion markers, or None if absent.

    Raises SieveParseError for a BEGIN marker with no END after it. Reading
    that as "no section" would make every comparison against it report
    nothing dropped, so it fails closed like any other unparsable section.
    """
    if not script:
        return None
    begin = script.find(SECTION_BEGIN)
    if begin == -1:
        return None
    end = script.find(SECTION_END, begin + len(SECTION_BEGIN))
    if end == -1:
        raise SieveParseError("ProtonFusion section has a BEGIN marker but no END marker")
    return script[begin + len(SECTION_BEGIN):end]


def section_body(script: str) -> str:
    """Return the rule text of a script: its marked section if it has one, else all of it."""
    section = extract_section(script)
    return section if section is not None else script


def collect_facts(rules: Iterable[ParsedRule]) -> Set[Fact]:
    facts: Set[Fact] = set()
    for rule in rules:
        facts |= rule.facts
    return facts


@dataclass
class SectionComparison:
    """Result of comparing a live section against a newly generated one."""
    live_rule_count: int
    new_rule_count: int
    live_fact_count: int
    new_fact_count: int
    dropped: List[Fact]
    added: List[Fact]
    opaque_live_rules: List[str]
    # (live fact, new fact) pairs where the live rule is the wildcard-less
    # begins-with / ends-with form older ProtonFusion versions generated and
    # the new section has the corrected pattern. Not counted as drops.
    wildcard_fixes: List[Tuple[Fact, Fact]] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        """True when every fact of the live section survives in the new one."""
        return not self.dropped

    def dropped_by_action(self) -> Dict[str, List[str]]:
        """Group dropped facts by their action set, for display."""
        grouped: Dict[str, List[str]] = {}
        for fact in self.dropped:
            key = " ".join(sorted(fact.actions)) or "(no actions)"
            cond = " AND ".join(sorted(describe_atom(a) for a in fact.conditions)) or "(always)"
            grouped.setdefault(key, []).append(cond)
        for conds in grouped.values():
            conds.sort()
        return dict(sorted(grouped.items()))


def _legacy_wildcard_variants(fact: Fact) -> Set[Fact]:
    """Corrected forms of a fact that may have been generated by the old begins/ends-with bug.

    Older ProtonFusion versions emitted "sender begins with news" as
    ``address :matches "From" "news"``, without the wildcard, which is an exact
    match. The current generator emits ``"news*"`` (or ``"*news"`` for ends
    with). Each :matches atom without a wildcard could be either, so this
    returns every fact obtained by rewriting one or more of them to
    ``value*`` / ``*value``. The originals are never in the result.

    A corrected pattern matches a superset of what the exact form matched and
    keeps the same actions, so a live fact whose corrected variant is in the
    new section has not lost any mail it used to handle.
    """
    options: List[List[Atom]] = []
    legacy = 0
    for atom in sorted(fact.conditions):
        if (len(atom) == 6 and atom[1] == ":matches"
                and "*" not in atom[5] and "?" not in atom[5] and atom[5]):
            literal = escape_match_literal(atom[5])
            options.append([
                atom,
                atom[:5] + (literal + "*",),
                atom[:5] + ("*" + literal,),
            ])
            legacy += 1
        else:
            options.append([atom])
    if legacy == 0 or legacy > _MAX_LEGACY_ATOMS:
        return set()
    variants = {Fact(frozenset(combo), fact.actions) for combo in itertools.product(*options)}
    variants.discard(fact)
    return variants


def compare_sections(live_script: str, new_script: str) -> SectionComparison:
    """Compare the rules of a live script's ProtonFusion section with a new one.

    ``live_script`` is the full live Sieve script (only its marked section is
    compared; user rules outside the markers are preserved by the merge and
    are not ProtonFusion's to drop). ``new_script`` may be a generated
    consolidated.sieve (no markers) or a full script with markers.

    Raises SieveParseError if either side cannot be parsed; callers should
    treat that as unsafe.
    """
    live_section = extract_section(live_script) or ""
    live_rules = parse_rules(live_section)
    new_rules = parse_rules(section_body(new_script))
    live_facts = collect_facts(live_rules)
    new_facts = collect_facts(new_rules)
    dropped = live_facts - new_facts
    added = new_facts - live_facts

    # Pair live rules in the old wildcard-less begins/ends-with form with their
    # corrected replacement, so the fix reads as one change rather than as a
    # drop plus an unrelated add.
    wildcard_fixes: List[Tuple[Fact, Fact]] = []
    for fact in sorted(dropped, key=Fact.describe):
        for variant in sorted(_legacy_wildcard_variants(fact), key=Fact.describe):
            if variant in new_facts:
                wildcard_fixes.append((fact, variant))
                break
    dropped -= {old for old, _ in wildcard_fixes}
    added -= {new for _, new in wildcard_fixes}

    return SectionComparison(
        live_rule_count=len(live_rules),
        new_rule_count=len(new_rules),
        live_fact_count=len(live_facts),
        new_fact_count=len(new_facts),
        dropped=sorted(dropped, key=Fact.describe),
        added=sorted(added, key=Fact.describe),
        opaque_live_rules=[r.text for r in live_rules if r.opaque],
        wildcard_fixes=wildcard_fixes,
    )


def script_facts(script: str) -> Set[Fact]:
    """Facts of a script's marked section (or of the whole script if unmarked)."""
    return collect_facts(parse_rules(section_body(script)))
