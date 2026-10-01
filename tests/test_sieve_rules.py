"""Tests for structural parsing/comparison of the ProtonFusion Sieve section."""

import pytest

from src.generator.sieve_generator import SieveGenerator, SECTION_BEGIN, SECTION_END
from src.generator.sieve_rules import (
    SieveParseError, compare_sections, extract_section, parse_rules, script_facts,
    validate_script,
)
from src.models.filter_models import (
    ConsolidatedFilter, ConditionGroup, FilterCondition, FilterAction,
    ConditionType, Operator, ActionType, LogicType,
)


def _wrap(body: str, before: str = "", after: str = "") -> str:
    """Build a full live script with a ProtonFusion section around `body`."""
    return f'require ["fileinto"];\n{before}{SECTION_BEGIN}\n{body}\n{SECTION_END}\n{after}'


def _sender_rule(senders, folder="Junk", op=Operator.IS):
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
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, values=["a", "b"]),
                ]),
                ConditionGroup(logic=LogicType.AND, conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="c"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, values=["d", "e"]),
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
        assert list(grouped) == ['fileinto "Junk";']
        assert any('"b"' in c for c in grouped['fileinto "Junk";'])

    def test_changed_action_detected(self):
        gen = SieveGenerator()
        live = SieveGenerator.merge_with_existing(gen.generate([_sender_rule(["a"], folder="Junk")]), "")
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

    def _new(self, op, value, folder="Junk"):
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
        live = _wrap('if address :matches "From" "news" { fileinto "Junk"; }')
        result = compare_sections(live, self._new(op, "news"))
        assert result.is_safe
        assert result.dropped == []
        assert result.added == []
        ((old, new),) = result.wildcard_fixes
        assert '"news"' in old.describe()
        assert f'"{new_value}"' in new.describe()

    def test_old_form_with_backslash_pairs_with_escaped_pattern(self):
        live = _wrap('if address :matches "From" "a\\\\b" { fileinto "Junk"; }')
        result = compare_sections(live, self._new(Operator.STARTS_WITH, "a\\b"))
        assert result.is_safe
        assert len(result.wildcard_fixes) == 1

    def test_old_form_with_different_action_is_still_a_drop(self):
        live = _wrap('if address :matches "From" "news" { fileinto "Junk"; }')
        result = compare_sections(live, self._new(Operator.STARTS_WITH, "news", folder="Work"))
        assert not result.is_safe
        assert result.wildcard_fixes == []
        assert len(result.dropped) == 1

    def test_old_form_without_replacement_is_still_a_drop(self):
        live = _wrap('if address :matches "From" "news" { fileinto "Junk"; }\n'
                     'if address :is "From" "a" { fileinto "Junk"; }')
        result = compare_sections(live, self._new(Operator.IS, "a"))
        assert not result.is_safe
        assert len(result.dropped) == 1

    def test_correction_does_not_hide_a_real_drop(self):
        live = _wrap('if address :matches "From" ["news", "gone"] { fileinto "Junk"; }')
        result = compare_sections(live, self._new(Operator.STARTS_WITH, "news"))
        assert len(result.wildcard_fixes) == 1
        assert [d.describe() for d in result.dropped] == ['address from :matches "gone"  ->  fileinto "Junk";']


class TestLegacySystemFolderActions:
    """Older versions wrote Move to Trash as discard, and Spam/Inbox by their labels."""

    def _new(self, action, value="a", op=Operator.IS):
        return SieveGenerator().generate([ConsolidatedFilter(
            name="r",
            condition_groups=[ConditionGroup(conditions=[
                FilterCondition(type=ConditionType.SENDER, operator=op, value=value)])],
            actions=[action],
        )])

    def test_trash_generates_fileinto_trash_never_discard(self):
        script = self._new(FilterAction(type=ActionType.TRASH))
        assert 'fileinto "trash";' in script
        assert "discard" not in script

    @pytest.mark.parametrize("old_action, new_action", [
        ("discard;", FilterAction(type=ActionType.TRASH)),
        ('fileinto "Spam";', FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "spam"})),
        ('fileinto "Inbox - Default";', FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "inbox"})),
    ])
    def test_old_action_reported_as_correction_not_drop(self, old_action, new_action):
        live = _wrap(f'if address :is "From" "a" {{ {old_action} }}')
        result = compare_sections(live, self._new(new_action))
        assert result.is_safe
        assert result.dropped == [] and result.added == []
        ((old, new),) = result.folder_fixes
        assert old_action in old.actions
        assert result.wildcard_fixes == []

    def test_old_action_and_old_wildcard_form_together(self):
        live = _wrap('if address :matches "From" "news" { discard; }')
        result = compare_sections(live, self._new(FilterAction(type=ActionType.TRASH), "news", Operator.STARTS_WITH))
        assert result.is_safe
        ((old, new),) = result.folder_fixes
        assert new.actions == frozenset({'fileinto "trash";'})
        assert '"news*"' in new.describe()

    def test_discard_replaced_by_other_folder_is_a_drop(self):
        live = _wrap('if address :is "From" "a" { discard; }')
        result = compare_sections(live, self._new(
            FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Junk"})))
        assert not result.is_safe
        assert result.folder_fixes == []
        assert len(result.dropped) == 1


class TestArchiveCase:
    """Older versions wrote fileinto "Archive"; it is the same action as "archive"."""

    def _new_archive(self):
        return SieveGenerator().generate([ConsolidatedFilter(
            name="r",
            condition_groups=[ConditionGroup(conditions=[
                FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a")])],
            actions=[FilterAction(type=ActionType.ARCHIVE)],
        )])

    def test_generator_writes_lowercase_archive(self):
        assert 'fileinto "archive";' in self._new_archive()

    @pytest.mark.parametrize("target", ["Archive", "archive", "ARCHIVE"])
    def test_any_case_compares_equal(self, target):
        live = _wrap(f'if address :is "From" "a" {{ fileinto "{target}"; }}')
        result = compare_sections(live, self._new_archive())
        assert result.is_safe
        assert result.dropped == [] and result.added == []
        assert result.folder_fixes == [] and result.wildcard_fixes == []

    def test_other_folder_case_still_matters(self):
        assert script_facts('if true { fileinto "Work"; }') != script_facts('if true { fileinto "work"; }')


class TestValidateScript:
    """validate_script: the check a merged script must pass before upload."""

    @pytest.mark.parametrize("script", [
        'require ["fileinto"];\nif address :is "From" "a" { fileinto "x"; }\n',
        'require "fileinto";\nrequire "imap4flags";\nif true { fileinto "x"; addflag "\\\\Seen"; }\n',
        '# comment only\n',
        '',
        'if true { keep; } elsif false { stop; } else { keep; }',
        'require ["vacation"];\nvacation :days 1 text:\nI am away.\n..dot-stuffed\n.\n;\n',
    ])
    def test_valid(self, script):
        validate_script(script)

    @pytest.mark.parametrize("script, needle", [
        ('if true { keep; }\nrequire "fileinto";\n', "before any other command"),
        ('if true { require "fileinto"; }', "before any other command"),
        ('if true { fileinto "x"; }', "without require 'fileinto'"),
        ('require ["fileinto"];\nif true { addflag "\\\\Seen"; }', "without require 'imap4flags'"),
        ('else { keep; }', "without a preceding 'if'"),
        ('require 5;', "string"),
        ('require ["fileinto"];\n"fileinto"];\nif true { keep; }', "expected 'ident'"),
        ('if true { keep;', "unterminated"),
        ('vacation text:\nno end\n', "unterminated"),
    ])
    def test_invalid(self, script, needle):
        with pytest.raises(SieveParseError, match=needle):
            validate_script(script)

    def test_text_literal_value(self):
        from src.generator.sieve_rules import _tokenize
        (tok,) = [t for t in _tokenize('text:\nline one\n..two\n.\n') if t.kind == "string"]
        assert tok.value == "line one\n.two\n"


def test_wildcard_fix_ignores_atoms_with_wildcards():
    """Only a wildcard-less :matches can be the old begins/ends-with form.

    The live "a*" already has a live wildcard. Escaping it and adding one
    gives "a\\**" (begins with the literal "a*"), a different and narrower
    rule, so the pair is a real drop, not a correction.
    """
    live = _wrap('if address :matches "From" "a*" { fileinto "Junk"; }')
    new = 'if address :matches "From" "a\\\\**" { fileinto "Junk"; }\n'
    result = compare_sections(live, new)
    assert result.wildcard_fixes == []
    assert [d.describe() for d in result.dropped] == ['address from :matches "a*"  ->  fileinto "Junk";']
    assert not result.is_safe
