"""Tests for filter parser."""

import pytest
import logging

from src.parser.filter_parser import (
    parse_condition_type, parse_operator, parse_action_type,
    parse_filter, parse_scraped_filters,
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

    def test_parse_partial_match(self):
        """Test partial matching."""
        assert parse_condition_type("sender address") == ConditionType.SENDER
        assert parse_condition_type("email from") == ConditionType.SENDER

    def test_parse_unknown_type(self, caplog):
        """Test parsing unknown type defaults to SENDER with warning."""
        with caplog.at_level(logging.WARNING):
            result = parse_condition_type("unknown_type")
        assert result == ConditionType.SENDER
        assert "Unknown condition type" in caplog.text


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

    def test_parse_partial_match(self):
        """Test partial matching."""
        assert parse_operator("string contains") == Operator.CONTAINS
        assert parse_operator("is equal") == Operator.IS

    def test_parse_unknown_operator(self, caplog):
        """Test parsing unknown operator defaults to CONTAINS with warning."""
        with caplog.at_level(logging.WARNING):
            result = parse_operator("unknown_op")
        assert result == Operator.CONTAINS
        assert "Unknown operator" in caplog.text


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
        ("move to trash", ActionType.DELETE),
        ("delete", ActionType.DELETE),
        ("permanently delete", ActionType.DELETE),
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

    def test_parse_partial_match(self):
        """Test partial matching."""
        assert parse_action_type("please move to folder") == ActionType.MOVE_TO
        assert parse_action_type("should archive this") == ActionType.ARCHIVE

    def test_parse_unknown_action(self, caplog):
        """Test parsing unknown action defaults to MOVE_TO with warning."""
        with caplog.at_level(logging.WARNING):
            result = parse_action_type("unknown_action")
        assert result == ActionType.MOVE_TO
        assert "Unknown action type" in caplog.text


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
        # Should parse the valid filters and log warning for the invalid one
        assert len(result) == 2
        assert "Failed to parse filter" in caplog.text

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
