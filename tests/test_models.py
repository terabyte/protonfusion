"""Tests for Pydantic models in filter_models.py and backup_models.py."""

import pytest
from datetime import datetime

from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, ConsolidatedFilter,
    ConditionGroup, ConditionType, Operator, ActionType, LogicType,
)
from src.models.backup_models import Backup, BackupMetadata


class TestFilterCondition:
    """Test FilterCondition model."""

    def test_create_condition(self):
        """Test creating a filter condition."""
        cond = FilterCondition(
            type=ConditionType.SENDER,
            operator=Operator.CONTAINS,
            value="spam@example.com"
        )
        assert cond.type == ConditionType.SENDER
        assert cond.operator == Operator.CONTAINS
        assert cond.value == "spam@example.com"

    def test_condition_default_value(self):
        """Test that value defaults to empty string."""
        cond = FilterCondition(
            type=ConditionType.SUBJECT,
            operator=Operator.IS
        )
        assert cond.value == ""

    def test_condition_serialization(self):
        """Test condition serialization to dict."""
        cond = FilterCondition(
            type=ConditionType.RECIPIENT,
            operator=Operator.IS,
            value="test@example.com"
        )
        data = cond.model_dump()
        assert data == {
            "type": "recipient",
            "operator": "is",
            "value": "test@example.com"
        }

    def test_condition_from_dict(self):
        """Test creating condition from dict."""
        data = {
            "type": "subject",
            "operator": "contains",
            "value": "urgent"
        }
        cond = FilterCondition.model_validate(data)
        assert cond.type == ConditionType.SUBJECT
        assert cond.operator == Operator.CONTAINS
        assert cond.value == "urgent"


class TestFilterAction:
    """Test FilterAction model."""

    def test_create_action_with_params(self):
        """Test creating action with parameters."""
        action = FilterAction(
            type=ActionType.MOVE_TO,
            parameters={"folder": "Spam"}
        )
        assert action.type == ActionType.MOVE_TO
        assert action.parameters == {"folder": "Spam"}

    def test_action_default_parameters(self):
        """Test that parameters defaults to empty dict."""
        action = FilterAction(type=ActionType.DELETE)
        assert action.parameters == {}

    def test_action_serialization(self):
        """Test action serialization."""
        action = FilterAction(
            type=ActionType.LABEL,
            parameters={"label": "Important"}
        )
        data = action.model_dump()
        assert data == {
            "type": "label",
            "parameters": {"label": "Important"}
        }

    def test_action_from_dict(self):
        """Test creating action from dict."""
        data = {
            "type": "mark_read",
            "parameters": {}
        }
        action = FilterAction.model_validate(data)
        assert action.type == ActionType.MARK_READ
        assert action.parameters == {}


class TestProtonMailFilter:
    """Test ProtonMailFilter model."""

    def test_create_basic_filter(self):
        """Test creating a basic filter."""
        f = ProtonMailFilter(name="Test Filter")
        assert f.name == "Test Filter"
        assert f.enabled is True
        assert f.priority == 0
        assert f.logic == LogicType.AND
        assert f.conditions == []
        assert f.actions == []

    def test_filter_with_all_fields(self):
        """Test filter with all fields populated."""
        f = ProtonMailFilter(
            name="Complex Filter",
            enabled=False,
            priority=5,
            logic=LogicType.OR,
            conditions=[
                FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam")
            ],
            actions=[
                FilterAction(type=ActionType.DELETE)
            ]
        )
        assert f.name == "Complex Filter"
        assert f.enabled is False
        assert f.priority == 5
        assert f.logic == LogicType.OR
        assert len(f.conditions) == 1
        assert len(f.actions) == 1

    def test_filter_serialization(self):
        """Test complete filter serialization."""
        f = ProtonMailFilter(
            name="Spam Filter",
            enabled=True,
            priority=1,
            logic=LogicType.AND,
            conditions=[
                FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam@test.com")
            ],
            actions=[
                FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Spam"})
            ]
        )
        data = f.model_dump()
        assert data["name"] == "Spam Filter"
        assert data["enabled"] is True
        assert data["priority"] == 1
        assert data["logic"] == "and"
        assert len(data["conditions"]) == 1
        assert len(data["actions"]) == 1

    def test_filter_from_dict(self):
        """Test creating filter from dict."""
        data = {
            "name": "Test",
            "enabled": False,
            "priority": 3,
            "logic": "or",
            "conditions": [
                {"type": "subject", "operator": "is", "value": "Test"}
            ],
            "actions": [
                {"type": "archive", "parameters": {}}
            ]
        }
        f = ProtonMailFilter.model_validate(data)
        assert f.name == "Test"
        assert f.enabled is False
        assert f.priority == 3
        assert f.logic == LogicType.OR
        assert len(f.conditions) == 1
        assert len(f.actions) == 1

    def test_filter_default_values(self):
        """Test that filter defaults are correct."""
        f = ProtonMailFilter(name="Minimal")
        assert f.enabled is True
        assert f.priority == 0
        assert f.logic == LogicType.AND
        assert f.conditions == []
        assert f.actions == []


class TestConsolidatedFilter:
    """Test ConsolidatedFilter model."""

    def test_create_consolidated_filter(self):
        """Test creating a consolidated filter."""
        cf = ConsolidatedFilter(name="Consolidated")
        assert cf.name == "Consolidated"
        assert cf.condition_groups == []
        assert cf.actions == []
        assert cf.source_filters == []
        assert cf.filter_count == 0

    def test_consolidated_filter_with_sources(self):
        """Test consolidated filter with source tracking."""
        cf = ConsolidatedFilter(
            name="Delete spam (consolidated from 3 filters)",
            condition_groups=[
                ConditionGroup(conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1"),
                ]),
                ConditionGroup(conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2"),
                ]),
            ],
            actions=[
                FilterAction(type=ActionType.DELETE)
            ],
            source_filters=["Filter 1", "Filter 2", "Filter 3"],
            filter_count=3
        )
        assert cf.filter_count == 3
        assert len(cf.source_filters) == 3
        assert cf.source_filters == ["Filter 1", "Filter 2", "Filter 3"]
        assert len(cf.condition_groups) == 2

    def test_consolidated_filter_serialization(self):
        """Test consolidated filter serialization."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[ConditionGroup(
                logic=LogicType.OR,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test"),
                ],
            )],
            source_filters=["A", "B"],
            filter_count=2
        )
        data = cf.model_dump()
        assert data["name"] == "Test"
        assert data["condition_groups"][0]["logic"] == "or"
        assert data["source_filters"] == ["A", "B"]
        assert data["filter_count"] == 2

    def test_condition_group_model(self):
        """Test ConditionGroup model."""
        group = ConditionGroup(
            logic=LogicType.AND,
            conditions=[
                FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
            ],
        )
        assert group.logic == LogicType.AND
        assert len(group.conditions) == 2

    def test_condition_group_defaults(self):
        """Test ConditionGroup default values."""
        group = ConditionGroup()
        assert group.logic == LogicType.AND
        assert group.conditions == []


class TestBackupMetadata:
    """Test BackupMetadata model."""

    def test_create_metadata(self):
        """Test creating backup metadata."""
        meta = BackupMetadata(
            filter_count=10,
            enabled_count=8,
            disabled_count=2,
            account_email="test@proton.me",
            tool_version="0.1.0"
        )
        assert meta.filter_count == 10
        assert meta.enabled_count == 8
        assert meta.disabled_count == 2
        assert meta.account_email == "test@proton.me"
        assert meta.tool_version == "0.1.0"

    def test_metadata_defaults(self):
        """Test metadata default values."""
        meta = BackupMetadata()
        assert meta.filter_count == 0
        assert meta.enabled_count == 0
        assert meta.disabled_count == 0
        assert meta.account_email == ""
        assert meta.tool_version == "0.1.0"


class TestBackup:
    """Test Backup model."""

    def test_create_backup(self):
        """Test creating a backup."""
        filters = [
            ProtonMailFilter(name="Filter 1"),
            ProtonMailFilter(name="Filter 2")
        ]
        backup = Backup(
            version="1.0",
            metadata=BackupMetadata(filter_count=2),
            filters=filters,
            checksum="sha256:abc123"
        )
        assert backup.version == "1.0"
        assert len(backup.filters) == 2
        assert backup.checksum == "sha256:abc123"

    def test_backup_defaults(self):
        """Test backup default values."""
        backup = Backup()
        assert backup.version == "1.0"
        assert isinstance(backup.timestamp, datetime)
        assert isinstance(backup.metadata, BackupMetadata)
        assert backup.filters == []
        assert backup.checksum == ""

    def test_backup_timestamp_default(self):
        """Test that timestamp is automatically generated."""
        backup = Backup()
        assert backup.timestamp is not None
        assert isinstance(backup.timestamp, datetime)

    def test_backup_serialization(self, sample_filters_list):
        """Test backup serialization."""
        backup = Backup(
            version="1.0",
            timestamp=datetime(2025, 1, 15, 12, 0, 0),
            metadata=BackupMetadata(filter_count=3),
            filters=sample_filters_list,
            checksum="sha256:test123"
        )
        data = backup.model_dump()
        assert data["version"] == "1.0"
        assert data["checksum"] == "sha256:test123"
        assert len(data["filters"]) == 3
        assert data["metadata"]["filter_count"] == 3

    def test_backup_from_dict(self):
        """Test creating backup from dict."""
        data = {
            "version": "1.0",
            "timestamp": "2025-01-15T12:00:00",
            "metadata": {
                "filter_count": 1,
                "enabled_count": 1,
                "disabled_count": 0,
                "account_email": "test@proton.me",
                "tool_version": "0.1.0"
            },
            "filters": [
                {
                    "name": "Test",
                    "enabled": True,
                    "priority": 0,
                    "logic": "and",
                    "conditions": [],
                    "actions": []
                }
            ],
            "checksum": "sha256:test"
        }
        backup = Backup.model_validate(data)
        assert backup.version == "1.0"
        assert len(backup.filters) == 1
        assert backup.metadata.filter_count == 1


class TestEnums:
    """Test enum values."""

    def test_condition_type_enum(self):
        """Test ConditionType enum values."""
        assert ConditionType.SENDER.value == "sender"
        assert ConditionType.RECIPIENT.value == "recipient"
        assert ConditionType.SUBJECT.value == "subject"
        assert ConditionType.ATTACHMENTS.value == "attachments"
        assert ConditionType.HEADER.value == "header"

    def test_operator_enum(self):
        """Test Operator enum values."""
        assert Operator.CONTAINS.value == "contains"
        assert Operator.IS.value == "is"
        assert Operator.MATCHES.value == "matches"
        assert Operator.STARTS_WITH.value == "starts_with"
        assert Operator.ENDS_WITH.value == "ends_with"
        assert Operator.HAS.value == "has"

    def test_action_type_enum(self):
        """Test ActionType enum values."""
        assert ActionType.MOVE_TO.value == "move_to"
        assert ActionType.LABEL.value == "label"
        assert ActionType.MARK_READ.value == "mark_read"
        assert ActionType.STAR.value == "star"
        assert ActionType.ARCHIVE.value == "archive"
        assert ActionType.DELETE.value == "delete"

    def test_logic_type_enum(self):
        """Test LogicType enum values."""
        assert LogicType.AND.value == "and"
        assert LogicType.OR.value == "or"
