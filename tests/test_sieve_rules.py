"""Tests for structural parsing/comparison of the ProtonFusion Sieve section."""

import pytest

from src.generator.sieve_generator import SieveGenerator, SECTION_BEGIN, SECTION_END
from src.generator.sieve_rules import (
    SieveParseError, compare_sections, extract_section, parse_rules, script_facts,
)
from src.models.filter_models import (
    ConsolidatedFilter, ConditionGroup, FilterCondition, FilterAction,
    ConditionType, Operator, ActionType, LogicType,
)


def _wrap(body: str, before: str = "", after: str = "") -> str:
    """Build a full live script with a ProtonFusion section around `body`."""
    return f'require ["fileinto"];\n{before}{SECTION_BEGIN}\n{body}\n{SECTION_END}\n{after}'


def _sender_rule(senders, folder="Spam", op=Operator.IS):
    return ConsolidatedFilter(
        name=f"to {folder}",
        condition_groups=[
            ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=op, value=s)])
            for s in senders
        ],
        actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": folder})],
        source_filters=[f"f-{s}" for s in senders],
        filter_count=len(senders),
    )


class TestParseRules:

    def test_simple_rule_one_fact_per_key(self):
        rules = parse_rules('if address :is "From" ["a@x.com", "b@x.com"] { discard; }')
        assert len(rules) == 1
        assert len(rules[0].facts) == 2
        assert all(f.actions == frozenset({"discard;"}) for f in rules[0].facts)

    def test_comments_ignored(self):
        text = (
            "# ProtonFusion - Filter Consolidation\n"
            "/* block comment */\n"
            'if header :contains "Subject" "x" { fileinto "A"; }  # trailing\n'
        )
        rules = parse_rules(text)
        assert len(rules) == 1

    def test_allof_is_cross_product(self):
        text = (
            'if allof (address :is "From" ["a", "b"], header :contains "Subject" "x") '
            '{ fileinto "A"; }'
        )
        facts = parse_rules(text)[0].facts
        assert len(facts) == 2
        for fact in facts:
            assert len(fact.conditions) == 2

    def test_anyof_nested_flattens(self):
        text = (
            'if anyof (address :is "From" "a", anyof (address :is "From" "b", '
            'header :contains "Subject" "c")) { discard; }'
        )
        assert len(parse_rules(text)[0].facts) == 3

    def test_default_comparator_is_case_insensitive(self):
        a = script_facts('if address :is "From" "Alice@X.com" { discard; }')
        b = script_facts('if address :is "from" "alice@x.com" { discard; }')
        assert a == b

    def test_folder_name_case_sensitive(self):
        a = script_facts('if address :is "From" "a" { fileinto "Work"; }')
        b = script_facts('if address :is "From" "a" { fileinto "work"; }')
        assert a != b

    def test_escaped_strings(self):
        facts = script_facts('if true { addflag "\\\\Seen"; }')
        (fact,) = facts
        assert fact.actions == frozenset({'addflag "\\\\Seen";'})

    def test_elsif_chain_is_opaque(self):
        text = 'if true { keep; } elsif false { discard; } else { stop; }'
        rules = parse_rules(text)
        assert len(rules) == 1
        assert rules[0].opaque

    def test_nested_if_is_opaque(self):
        rules = parse_rules('if true { if false { discard; } }')
        assert rules[0].opaque

    def test_unmodelled_test_is_opaque_atom(self):
        (fact,) = script_facts('if not exists "X-Foo" { discard; }')
        (atom,) = fact.conditions
        assert atom[0] == "opaque"

    def test_unconditional_actions(self):
        rules = parse_rules("keep;\nstop;\n")
        assert len(rules) == 2

    def test_require_inside_section_skipped(self):
        assert parse_rules('require ["fileinto"]; if true { keep; }')[0].facts

    @pytest.mark.parametrize("bad", [
        'if true { keep; ',
        'if address "From" "unterminated { keep; }',
        'if true { keep; } }',
        'if true keep;',
    ])
    def test_parse_errors(self, bad):
        with pytest.raises(SieveParseError):
            parse_rules(bad)

    def test_round_trips_generator_output(self):
        """Every generator construct parses to the facts we expect."""
        cf = ConsolidatedFilter(
            name="mixed",
            condition_groups=[
                ConditionGroup(conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a|b"),
                ]),
                ConditionGroup(logic=LogicType.AND, conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="c"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="d, e"),
                ]),
                ConditionGroup(logic=LogicType.OR, conditions=[
                    FilterCondition(type=ConditionType.RECIPIENT, operator=Operator.IS, value="f"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.MATCHES, value="g*"),
                ]),
            ],
            actions=[
                FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Work"}),
                FilterAction(type=ActionType.MARK_READ),
            ],
            source_filters=["x"],
            filter_count=3,
        )
        script = SieveGenerator().generate([cf])
        facts = script_facts(script)
        # a, b, (c AND d), (c AND e), f, g*
        assert len(facts) == 6
        assert all(f.actions == frozenset({'fileinto "Work";', 'addflag "\\\\Seen";'}) for f in facts)


class TestExtractSection:

    def test_extracts_between_markers(self):
        script = _wrap("keep;", before="# before\n", after="# after\n")
        assert extract_section(script).strip() == "keep;"

    def test_none_without_markers(self):
        assert extract_section("keep;") is None
        assert extract_section("") is None

    def test_begin_without_end_raises(self):
        """A truncated section must fail closed, not read as absent."""
        with pytest.raises(SieveParseError):
            extract_section(f"{SECTION_BEGIN}\nkeep;")

    def test_compare_against_truncated_live_section_raises(self):
        with pytest.raises(SieveParseError):
            compare_sections(f"{SECTION_BEGIN}\nkeep;", "keep;")


class TestCompareSections:

    def test_identical_is_safe(self):
        new = SieveGenerator().generate([_sender_rule(["a", "b"])])
        live = SieveGenerator.merge_with_existing(new, "")
        result = compare_sections(live, new)
        assert result.is_safe
        assert result.added == []

    def test_regrouping_is_safe(self):
        """Merging two rules with the same action into one array is not a drop."""
        gen = SieveGenerator()
        live = SieveGenerator.merge_with_existing(
            gen.generate([_sender_rule(["a"]), _sender_rule(["b"])]), "")
        new = gen.generate([_sender_rule(["b", "a"])])
        assert compare_sections(live, new).is_safe

    def test_dropped_sender_detected(self):
        gen = SieveGenerator()
        live = SieveGenerator.merge_with_existing(gen.generate([_sender_rule(["a", "b", "c"])]), "")
        new = gen.generate([_sender_rule(["a"])])
        result = compare_sections(live, new)
        assert not result.is_safe
        assert len(result.dropped) == 2
        grouped = result.dropped_by_action()
        assert list(grouped) == ['fileinto "Spam";']
        assert any('"b"' in c for c in grouped['fileinto "Spam";'])

    def test_changed_action_detected(self):
        gen = SieveGenerator()
        live = SieveGenerator.merge_with_existing(gen.generate([_sender_rule(["a"], folder="Spam")]), "")
        new = gen.generate([_sender_rule(["a"], folder="Work")])
        result = compare_sections(live, new)
        assert len(result.dropped) == 1
        assert len(result.added) == 1

    def test_section_rebuilt_from_few_ui_filters_is_unsafe(self):
        """A large live section regenerated from only a handful of UI filters is refused."""
        gen = SieveGenerator()
        live_rules = [_sender_rule([f"s{i}@x.com"], folder=f"F{i % 7}") for i in range(200)]
        live = SieveGenerator.merge_with_existing(gen.generate(live_rules), "")
        new = gen.generate(live_rules[:10])
        result = compare_sections(live, new)
        assert not result.is_safe
        assert len(result.dropped) == 190

    def test_user_rules_outside_markers_not_compared(self):
        new = SieveGenerator().generate([_sender_rule(["a"])])
        live = SieveGenerator.merge_with_existing(
            new, 'if header :contains "Subject" "mine" { keep; stop; }\n')
        assert compare_sections(live, new).is_safe

    def test_no_live_section_is_safe(self):
        new = SieveGenerator().generate([_sender_rule(["a"])])
        assert compare_sections('if true { keep; }', new).is_safe
        assert compare_sections("", new).is_safe

    def test_opaque_rule_must_survive_verbatim(self):
        new = SieveGenerator().generate([_sender_rule(["a"])])
        live = _wrap('if true { keep; } else { discard; }')
        result = compare_sections(live, new)
        assert not result.is_safe
        assert result.opaque_live_rules

    def test_new_script_with_markers_uses_section(self):
        new = SieveGenerator.merge_with_existing(
            SieveGenerator().generate([_sender_rule(["a"])]),
            'if address :is "From" "zzz" { discard; }\n')
        live = SieveGenerator.merge_with_existing(
            SieveGenerator().generate([_sender_rule(["a"])]), "")
        # zzz is outside the new markers, so it is not counted as "added"
        result = compare_sections(live, new)
        assert result.is_safe
        assert result.added == []

    def test_unparseable_live_raises(self):
        with pytest.raises(SieveParseError):
            compare_sections(_wrap("if true {"), "keep;")


class TestLegacyWildcardForm:
    """Older versions wrote begins/ends-with as :matches without the wildcard."""

    def _new(self, op, value, folder="Spam"):
        return SieveGenerator().generate([_sender_rule([value], folder=folder, op=op)])

    def test_generated_begins_with_has_wildcard(self):
        (fact,) = script_facts(self._new(Operator.STARTS_WITH, "News"))
        (atom,) = fact.conditions
        assert atom[1] == ":matches"
        assert atom[5] == "news*"

    @pytest.mark.parametrize("op, new_value", [
        (Operator.STARTS_WITH, "news*"),
        (Operator.ENDS_WITH, "*news"),
    ])
    def test_old_form_reported_as_correction_not_drop(self, op, new_value):
        live = _wrap('if address :matches "From" "news" { fileinto "Spam"; }')
        result = compare_sections(live, self._new(op, "news"))
        assert result.is_safe
        assert result.dropped == []
        assert result.added == []
        ((old, new),) = result.wildcard_fixes
        assert '"news"' in old.describe()
        assert f'"{new_value}"' in new.describe()

    def test_old_form_with_backslash_pairs_with_escaped_pattern(self):
        live = _wrap('if address :matches "From" "a\\\\b" { fileinto "Spam"; }')
        result = compare_sections(live, self._new(Operator.STARTS_WITH, "a\\b"))
        assert result.is_safe
        assert len(result.wildcard_fixes) == 1

    def test_old_form_with_different_action_is_still_a_drop(self):
        live = _wrap('if address :matches "From" "news" { fileinto "Spam"; }')
        result = compare_sections(live, self._new(Operator.STARTS_WITH, "news", folder="Work"))
        assert not result.is_safe
        assert result.wildcard_fixes == []
        assert len(result.dropped) == 1

    def test_old_form_without_replacement_is_still_a_drop(self):
        live = _wrap('if address :matches "From" "news" { fileinto "Spam"; }\n'
                     'if address :is "From" "a" { fileinto "Spam"; }')
        result = compare_sections(live, self._new(Operator.IS, "a"))
        assert not result.is_safe
        assert len(result.dropped) == 1

    def test_correction_does_not_hide_a_real_drop(self):
        live = _wrap('if address :matches "From" ["news", "gone"] { fileinto "Spam"; }')
        result = compare_sections(live, self._new(Operator.STARTS_WITH, "news"))
        assert len(result.wildcard_fixes) == 1
        assert [d.describe() for d in result.dropped] == ['address from :matches "gone"  ->  fileinto "Spam";']
