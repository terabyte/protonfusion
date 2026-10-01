"""Tests for filter parser."""

import pytest
import logging

from src.parser.filter_parser import (
    parse_condition_type, parse_operator, parse_action_type,
    parse_filter, parse_scraped_filters, UnknownFilterValueError,
    CONDITION_TYPE_MAP, OPERATOR_MAP, ACTION_TYPE_MAP,
)
from src.models.filter_models import (
    ConditionType, Operator, ActionType, LogicType,
    ProtonMailFilter,
)


class TestParseConditionType:
    """Test condition type parsing."""

    @pytest.mark.parametrize("raw,expected", [
        ("sender", ConditionType.SENDER),
        ("from", ConditionType.SENDER),
        ("recipient", ConditionType.RECIPIENT),
        ("to", ConditionType.RECIPIENT),
        ("subject", ConditionType.SUBJECT),
        ("attachments", ConditionType.ATTACHMENTS),
        ("has attachment", ConditionType.ATTACHMENTS),
        ("header", ConditionType.HEADER),
    ])
    def test_parse_known_types(self, raw, expected):
        """Test parsing known condition types."""
        assert parse_condition_type(raw) == expected

    def test_parse_case_insensitive(self):
        """Test that parsing is case-insensitive."""
        assert parse_condition_type("SENDER") == ConditionType.SENDER
        assert parse_condition_type("From") == ConditionType.SENDER
        assert parse_condition_type("SUBJECT") == ConditionType.SUBJECT

    def test_parse_with_whitespace(self):
        """Test parsing with extra whitespace."""
        assert parse_condition_type("  sender  ") == ConditionType.SENDER
        assert parse_condition_type("\trecipient\n") == ConditionType.RECIPIENT

    @pytest.mark.parametrize("raw", ["unknown_type", "body", "sender address", "", None])
    def test_parse_unknown_type_raises(self, raw):
        """No default and no substring guess: an unknown type is an error."""
        with pytest.raises(UnknownFilterValueError) as exc:
            parse_condition_type(raw)
        assert exc.value.field == "condition type"
        assert exc.value.value == raw


class TestParseOperator:
    """Test operator parsing."""

    @pytest.mark.parametrize("raw,expected", [
        ("contains", Operator.CONTAINS),
        ("is", Operator.IS),
        ("is exactly", Operator.IS),
        ("matches", Operator.MATCHES),
        ("starts with", Operator.STARTS_WITH),
        ("ends with", Operator.ENDS_WITH),
        ("has", Operator.HAS),
    ])
    def test_parse_known_operators(self, raw, expected):
        """Test parsing known operators."""
        assert parse_operator(raw) == expected

    def test_parse_case_insensitive(self):
        """Test that parsing is case-insensitive."""
        assert parse_operator("CONTAINS") == Operator.CONTAINS
        assert parse_operator("Is Exactly") == Operator.IS
        assert parse_operator("MATCHES") == Operator.MATCHES

    def test_parse_with_whitespace(self):
        """Test parsing with extra whitespace."""
        assert parse_operator("  contains  ") == Operator.CONTAINS
        assert parse_operator("\tstarts with\n") == Operator.STARTS_WITH

    @pytest.mark.parametrize("raw,expected", [
        ("starts_with", Operator.STARTS_WITH),
        ("ends_with", Operator.ENDS_WITH),
        ("begins with", Operator.STARTS_WITH),
    ])
    def test_parse_scraper_model_values(self, raw, expected):
        """The scraper emits model values; these used to fall through to CONTAINS."""
        assert parse_operator(raw) == expected

    @pytest.mark.parametrize("raw", ["unknown_op", "is not", "does not contain", "", None])
    def test_parse_unknown_operator_raises(self, raw):
        """A substring match would read "is not" as IS, inverting the condition."""
        with pytest.raises(UnknownFilterValueError) as exc:
            parse_operator(raw)
        assert exc.value.field == "operator"


class TestParseActionType:
    """Test action type parsing."""

    @pytest.mark.parametrize("raw,expected", [
        ("move to", ActionType.MOVE_TO),
        ("move_to", ActionType.MOVE_TO),
        ("move message to", ActionType.MOVE_TO),
        ("apply label", ActionType.LABEL),
        ("label", ActionType.LABEL),
        ("mark as read", ActionType.MARK_READ),
        ("mark_read", ActionType.MARK_READ),
        ("star", ActionType.STAR),
        ("star it", ActionType.STAR),
        ("archive", ActionType.ARCHIVE),
        ("move to archive", ActionType.ARCHIVE),
        ("move to trash", ActionType.TRASH),
        ("trash", ActionType.TRASH),
        ("delete", ActionType.TRASH),
    ])
    def test_parse_known_actions(self, raw, expected):
        """Test parsing known action types."""
        assert parse_action_type(raw) == expected

    def test_parse_case_insensitive(self):
        """Test that parsing is case-insensitive."""
        assert parse_action_type("MOVE TO") == ActionType.MOVE_TO
        assert parse_action_type("Delete") == ActionType.DELETE
        assert parse_action_type("ARCHIVE") == ActionType.ARCHIVE

    def test_parse_with_whitespace(self):
        """Test parsing with extra whitespace."""
        assert parse_action_type("  label  ") == ActionType.LABEL
        assert parse_action_type("\tdelete\n") == ActionType.DELETE

    @pytest.mark.parametrize("raw", ["unknown_action", "forward", "please move to folder", None])
    def test_parse_unknown_action_raises(self, raw):
        """Test parsing an unknown action type is an error, not MOVE_TO."""
        with pytest.raises(UnknownFilterValueError) as exc:
            parse_action_type(raw)
        assert exc.value.field == "action type"


class TestParseFilter:
    """Test parsing complete filter objects."""

    def test_parse_basic_filter(self):
        """Test parsing a basic filter."""
        raw = {
            "name": "Test Filter",
            "enabled": True,
            "priority": 1,
            "logic": "and",
            "conditions": [
                {"type": "sender", "operator": "contains", "value": "test@example.com"}
            ],
            "actions": [
                {"type": "move to", "parameters": {"folder": "Test"}}
            ]
        }
        result = parse_filter(raw)
        assert isinstance(result, ProtonMailFilter)
        assert result.name == "Test Filter"
        assert result.enabled is True
        assert result.priority == 1
        assert result.logic == LogicType.AND
        assert len(result.conditions) == 1
        assert len(result.actions) == 1

    def test_parse_filter_with_or_logic(self):
        """Test parsing filter with OR logic."""
        raw = {
            "name": "OR Filter",
            "logic": "or",
            "conditions": [],
            "actions": []
        }
        result = parse_filter(raw)
        assert result.logic == LogicType.OR

    def test_parse_filter_defaults_to_and(self):
        """Test that logic defaults to AND."""
        raw = {
            "name": "Default Logic",
            "conditions": [],
            "actions": []
        }
        result = parse_filter(raw)
        assert result.logic == LogicType.AND

    def test_parse_filter_with_multiple_conditions(self):
        """Test parsing filter with multiple conditions."""
        raw = {
            "name": "Multi Condition",
            "conditions": [
                {"type": "sender", "operator": "contains", "value": "spam"},
                {"type": "subject", "operator": "contains", "value": "urgent"},
                {"type": "recipient", "operator": "is", "value": "me@test.com"}
            ],
            "actions": [
                {"type": "delete", "parameters": {}}
            ]
        }
        result = parse_filter(raw)
        assert len(result.conditions) == 3
        assert result.conditions[0].type == ConditionType.SENDER
        assert result.conditions[1].type == ConditionType.SUBJECT
        assert result.conditions[2].type == ConditionType.RECIPIENT

    def test_parse_filter_with_multiple_actions(self):
        """Test parsing filter with multiple actions."""
        raw = {
            "name": "Multi Action",
            "conditions": [],
            "actions": [
                {"type": "label", "parameters": {"label": "Important"}},
                {"type": "mark as read", "parameters": {}},
                {"type": "star", "parameters": {}}
            ]
        }
        result = parse_filter(raw)
        assert len(result.actions) == 3
        assert result.actions[0].type == ActionType.LABEL
        assert result.actions[1].type == ActionType.MARK_READ
        assert result.actions[2].type == ActionType.STAR

    def test_parse_filter_missing_fields(self):
        """Test parsing filter with missing fields uses defaults."""
        raw = {}
        result = parse_filter(raw)
        assert result.name == "Unknown Filter"
        assert result.enabled is True
        assert result.priority == 0
        assert result.logic == LogicType.AND
        assert result.conditions == []
        assert result.actions == []

    def test_parse_filter_empty_conditions(self):
        """Test parsing filter with empty conditions list."""
        raw = {
            "name": "No Conditions",
            "conditions": [],
            "actions": [{"type": "archive"}]
        }
        result = parse_filter(raw)
        assert result.conditions == []

    def test_parse_filter_empty_actions(self):
        """Test parsing filter with empty actions list."""
        raw = {
            "name": "No Actions",
            "conditions": [{"type": "sender", "operator": "contains", "value": "test"}],
            "actions": []
        }
        result = parse_filter(raw)
        assert result.actions == []

    def test_parse_filter_disabled(self):
        """Test parsing disabled filter."""
        raw = {
            "name": "Disabled",
            "enabled": False,
            "conditions": [],
            "actions": []
        }
        result = parse_filter(raw)
        assert result.enabled is False

    def test_parse_filter_with_priority(self):
        """Test parsing filter with priority."""
        raw = {
            "name": "High Priority",
            "priority": 10,
            "conditions": [],
            "actions": []
        }
        result = parse_filter(raw)
        assert result.priority == 10


class TestParseScrapedFilters:
    """Test parsing lists of scraped filters."""

    def test_parse_empty_list(self):
        """Test parsing empty filter list."""
        result = parse_scraped_filters([])
        assert result == []

    def test_parse_single_filter(self):
        """Test parsing single filter."""
        raw = [{
            "name": "Single Filter",
            "conditions": [{"type": "sender", "operator": "contains", "value": "test"}],
            "actions": [{"type": "delete"}]
        }]
        result = parse_scraped_filters(raw)
        assert len(result) == 1
        assert result[0].name == "Single Filter"

    def test_parse_multiple_filters(self, raw_filters_list):
        """Test parsing multiple filters."""
        result = parse_scraped_filters(raw_filters_list)
        assert len(result) == 2
        assert result[0].name == "Test Filter"
        assert result[1].name == "Another Filter"

    def test_parse_filters_with_errors(self, caplog):
        """Test that parsing continues even if some filters fail."""
        raw = [
            {"name": "Good Filter", "conditions": [], "actions": []},
            None,  # This will cause an error
            {"name": "Another Good", "conditions": [], "actions": []}
        ]
        with caplog.at_level(logging.WARNING):
            result = parse_scraped_filters(raw)
        # The invalid one is kept as a flagged stub, not dropped
        assert [f.name for f in result] == ["Good Filter", "Unparseable filter #2", "Another Good"]
        assert result[0].is_complete and result[2].is_complete
        assert not result[1].is_complete
        assert "Failed to parse filter" in caplog.text

    def test_unparseable_filter_kept_as_flagged_stub(self):
        """A filter parse_filter rejects keeps its name, state, position and raw data."""
        raw = {
            "name": "Broken",
            "enabled": False,
            "priority": 7,
            "conditions": [None],  # parse_filter cannot read this entry at all
            "actions": [],
            "raw": {"conditions_text": "the sender", "actions_text": "", "sieve_text": ""},
            "scrape_issues": ["label row unreadable"],
        }
        [stub] = parse_scraped_filters([raw])
        assert stub.name == "Broken"
        assert stub.enabled is False
        assert stub.priority == 7
        assert stub.conditions == [] and stub.actions == []
        assert stub.raw.conditions_text == "the sender"
        assert stub.scrape_issues[0] == "label row unreadable"
        assert "could not be parsed" in stub.scrape_issues[1]
        assert '"conditions": [null]' in stub.scrape_issues[1]

    def test_unparseable_stub_with_unreadable_state_counts_as_enabled(self):
        """cleanup only deletes disabled filters, so an unknown state keeps it safe."""
        [stub] = parse_scraped_filters([{"name": "X", "enabled": "maybe", "conditions": [None]}])
        assert stub.enabled is True
        assert not stub.is_complete

    def test_parse_filters_logs_summary(self, caplog):
        """Test that parsing logs a summary."""
        raw = [
            {"name": "Filter 1", "conditions": [], "actions": []},
            {"name": "Filter 2", "conditions": [], "actions": []},
        ]
        with caplog.at_level(logging.INFO):
            result = parse_scraped_filters(raw)
        assert "Parsed 2/2 filters successfully" in caplog.text

    def test_parse_filters_preserves_order(self):
        """Test that filter order is preserved."""
        raw = [
            {"name": "First", "conditions": [], "actions": []},
            {"name": "Second", "conditions": [], "actions": []},
            {"name": "Third", "conditions": [], "actions": []},
        ]
        result = parse_scraped_filters(raw)
        assert result[0].name == "First"
        assert result[1].name == "Second"
        assert result[2].name == "Third"

    def test_parse_real_world_example(self):
        """Test parsing a realistic scraped filter."""
        raw = [{
            "name": "Newsletter to Archive",
            "enabled": True,
            "priority": 5,
            "logic": "or",
            "conditions": [
                {"type": "from", "operator": "contains", "value": "newsletter@"},
                {"type": "subject", "operator": "contains", "value": "unsubscribe"},
            ],
            "actions": [
                {"type": "move message to", "parameters": {"folder": "Newsletters"}},
                {"type": "mark as read", "parameters": {}}
            ]
        }]
        result = parse_scraped_filters(raw)
        assert len(result) == 1
        f = result[0]
        assert f.name == "Newsletter to Archive"
        assert f.logic == LogicType.OR
        assert len(f.conditions) == 2
        assert len(f.actions) == 2
        assert f.actions[0].type == ActionType.MOVE_TO
        assert f.actions[1].type == ActionType.MARK_READ


class TestParseEvidence:
    """Raw evidence and scrape issues pass through the parser unchanged."""

    def test_evidence_passthrough(self):
        f = parse_filter({
            "name": "X",
            "raw": {"conditions_text": "c", "actions_text": "a", "sieve_text": ""},
            "scrape_issues": ["unknown action row 'filter-modal:foo-row'"],
        })
        assert f.raw.actions_text == "a"
        assert f.scrape_issues == ["unknown action row 'filter-modal:foo-row'"]
        assert not f.is_complete

    def test_missing_evidence_defaults(self):
        f = parse_filter({"name": "X"})
        assert f.raw is None
        assert f.is_complete

    def test_sieve_flag_passthrough(self):
        f = parse_filter({"name": "S", "raw": {"sieve_text": ""}, "is_sieve": True})
        assert f.is_sieve is True

    def test_sieve_flag_absent_derives_from_script(self):
        f = parse_filter({"name": "S", "raw": {"sieve_text": "keep;"}})
        assert f.is_sieve is True


class TestUnknownValues:
    """Unknown or missing condition/action values are never guessed at."""

    DELETE_RULE = {
        "name": "Delete Promos",
        "logic": "and",
        "conditions": [
            {"type": "sender", "operator": "is", "value": "promo@shop.example"},
            {"type": "body", "operator": "contains", "value": "sale"},
        ],
        "actions": [{"type": "delete", "parameters": {}}],
    }

    def test_strict_parse_names_filter_and_value(self):
        with pytest.raises(UnknownFilterValueError) as exc:
            parse_filter(self.DELETE_RULE)
        assert exc.value.filter_name == "Delete Promos"
        assert exc.value.value == "body"
        assert "'Delete Promos'" in str(exc.value) and "'body'" in str(exc.value)

    @pytest.mark.parametrize("cond,act,needle", [
        ({"operator": "contains", "value": "x"}, {"type": "delete"}, "missing condition type"),
        ({"type": "sender", "value": "x"}, {"type": "delete"}, "missing operator"),
        ({"type": "sender", "operator": "contains", "value": "x"}, {"parameters": {}}, "missing action type"),
        ({"type": "sender", "operator": "contains", "value": "x"}, {"type": "forward"}, "unknown action type 'forward'"),
    ])
    def test_missing_keys_raise(self, cond, act, needle):
        """Keys that used to default (sender/contains/move_to) are errors too."""
        with pytest.raises(UnknownFilterValueError, match=needle):
            parse_filter({"name": "F", "conditions": [cond], "actions": [act]})

    def test_unknown_logic_raises(self):
        with pytest.raises(UnknownFilterValueError, match="unknown logic 'xor'"):
            parse_filter({"name": "F", "logic": "xor"})

    def test_scraped_list_keeps_filter_flagged_incomplete(self, caplog):
        """parse_scraped_filters neither crashes, drops, nor guesses: the
        bad entry is removed and recorded, so the filter is incomplete."""
        with caplog.at_level(logging.WARNING):
            result = parse_scraped_filters([self.DELETE_RULE])
        assert len(result) == 1
        f = result[0]
        assert not f.is_complete
        assert [c.value for c in f.conditions] == ["promo@shop.example"]
        assert f.actions[0].type == ActionType.TRASH
        assert len(f.scrape_issues) == 1
        assert "unknown condition type 'body'" in f.scrape_issues[0]
        assert '"value": "sale"' in f.scrape_issues[0]
        assert "Delete Promos" in caplog.text

    def test_scraped_list_unknown_action_and_logic(self):
        f = parse_scraped_filters([{
            "name": "F", "logic": "xor",
            "conditions": [{"type": "sender", "operator": "contains", "value": "a"}],
            "actions": [{"type": "forward", "parameters": {"to": "x@y"}}],
        }])[0]
        assert f.actions == []
        assert f.logic == LogicType.AND
        assert any("unknown action type 'forward'" in i for i in f.scrape_issues)
        assert any("unknown logic 'xor'" in i for i in f.scrape_issues)

    def test_existing_scrape_issues_kept(self):
        raw = dict(self.DELETE_RULE, scrape_issues=["condition 2: unknown condition type 'Body'"])
        f = parse_scraped_filters([raw])[0]
        assert f.scrape_issues[0] == "condition 2: unknown condition type 'Body'"
        assert len(f.scrape_issues) == 2


def test_permanently_delete_is_not_a_known_action():
    """Proton's wizard has no permanent delete; the label is never guessed at."""
    with pytest.raises(UnknownFilterValueError):
        parse_action_type("permanently delete")


def test_scraped_empty_value_is_quarantined():
    """Through parse_scraped_filters: kept, flagged incomplete, condition dropped."""
    (f,) = parse_scraped_filters([{
        "name": "Blank subject",
        "conditions": [{"type": "subject", "operator": "contains", "value": ""}],
        "actions": [{"type": "trash", "parameters": {}}],
    }])
    assert not f.is_complete
    assert f.conditions == []
    assert any("empty value" in issue for issue in f.scrape_issues)


def test_scraped_chip_list_passes_through():
    (f,) = parse_scraped_filters([{
        "name": "chips",
        "conditions": [{"type": "subject", "operator": "contains", "values": ["Invoice, Receipt", "Bill"]}],
        "actions": [{"type": "trash"}],
    }])
    assert f.is_complete
    assert f.conditions[0].values == ["Invoice, Receipt", "Bill"]
