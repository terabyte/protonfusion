"""Tests for Pydantic models in filter_models.py and backup_models.py."""

import pytest
from datetime import datetime

from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, ConsolidatedFilter,
    ConditionGroup, ConditionType, Operator, ActionType, LogicType, FilterStatus,
)
from src.models.backup_models import Backup, BackupMetadata, ArchiveEntry, Archive


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

    def test_filter_status_enum(self):
        """Test FilterStatus enum values."""
        assert FilterStatus.ENABLED.value == "enabled"
        assert FilterStatus.DISABLED.value == "disabled"
        assert FilterStatus.ARCHIVED.value == "archived"
        assert FilterStatus.DEPRECATED.value == "deprecated"


class TestFilterStatus:
    """Test FilterStatus integration with ProtonMailFilter."""

    def test_default_status_is_enabled(self):
        """Test that default status is ENABLED."""
        f = ProtonMailFilter(name="Test")
        assert f.status == FilterStatus.ENABLED
        assert f.enabled is True

    def test_backward_compat_no_status_enabled(self):
        """Test backward compat: no status field, enabled=True -> ENABLED."""
        data = {"name": "Test", "enabled": True}
        f = ProtonMailFilter.model_validate(data)
        assert f.status == FilterStatus.ENABLED
        assert f.enabled is True

    def test_backward_compat_no_status_disabled(self):
        """Test backward compat: no status field, enabled=False -> DISABLED."""
        data = {"name": "Test", "enabled": False}
        f = ProtonMailFilter.model_validate(data)
        assert f.status == FilterStatus.DISABLED
        assert f.enabled is False

    def test_status_archived_sets_enabled_false(self):
        """Test that ARCHIVED status sets enabled=False."""
        f = ProtonMailFilter(name="Test", status=FilterStatus.ARCHIVED)
        assert f.enabled is False

    def test_status_deprecated_sets_enabled_false(self):
        """Test that DEPRECATED status sets enabled=False."""
        f = ProtonMailFilter(name="Test", status=FilterStatus.DEPRECATED)
        assert f.enabled is False

    def test_status_enabled_sets_enabled_true(self):
        """Test that ENABLED status sets enabled=True."""
        f = ProtonMailFilter(name="Test", status=FilterStatus.ENABLED)
        assert f.enabled is True

    def test_status_disabled_sets_enabled_false(self):
        """Test that DISABLED status sets enabled=False."""
        f = ProtonMailFilter(name="Test", status=FilterStatus.DISABLED)
        assert f.enabled is False

    def test_content_hash_excludes_status(self):
        """Test that content_hash is the same regardless of status."""
        f_enabled = ProtonMailFilter(
            name="Test",
            status=FilterStatus.ENABLED,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="x")],
            actions=[FilterAction(type=ActionType.DELETE)],
        )
        f_archived = ProtonMailFilter(
            name="Test",
            status=FilterStatus.ARCHIVED,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="x")],
            actions=[FilterAction(type=ActionType.DELETE)],
        )
        assert f_enabled.content_hash == f_archived.content_hash

    def test_status_from_string(self):
        """Test creating filter with status as string."""
        data = {"name": "Test", "status": "archived"}
        f = ProtonMailFilter.model_validate(data)
        assert f.status == FilterStatus.ARCHIVED
        assert f.enabled is False

    def test_status_serialization_roundtrip(self):
        """Test that status survives serialization/deserialization."""
        f = ProtonMailFilter(name="Test", status=FilterStatus.ARCHIVED)
        data = f.model_dump()
        f2 = ProtonMailFilter.model_validate(data)
        assert f2.status == FilterStatus.ARCHIVED
        assert f2.enabled is False


class TestArchiveEntry:
    """Test ArchiveEntry model."""

    def test_create_archive_entry(self):
        """Test creating an archive entry."""
        f = ProtonMailFilter(name="Test", status=FilterStatus.ARCHIVED)
        entry = ArchiveEntry(
            filter=f,
            archived_at="2025-06-15T10:00:00+00:00",
            source_snapshot="2025-06-15_10-00-00",
        )
        assert entry.filter.name == "Test"
        assert entry.archived_at == "2025-06-15T10:00:00+00:00"
        assert entry.source_snapshot == "2025-06-15_10-00-00"

    def test_archive_entry_defaults(self):
        """Test archive entry default values."""
        f = ProtonMailFilter(name="Test")
        entry = ArchiveEntry(filter=f)
        assert entry.archived_at == ""
        assert entry.source_snapshot == ""

    def test_archive_entry_serialization_roundtrip(self):
        """Test archive entry serialization/deserialization."""
        f = ProtonMailFilter(
            name="Test",
            status=FilterStatus.ARCHIVED,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="x")],
            actions=[FilterAction(type=ActionType.DELETE)],
        )
        entry = ArchiveEntry(filter=f, archived_at="2025-01-01T00:00:00Z", source_snapshot="snap1")
        data = entry.model_dump()
        entry2 = ArchiveEntry.model_validate(data)
        assert entry2.filter.name == "Test"
        assert entry2.filter.status == FilterStatus.ARCHIVED
        assert entry2.archived_at == "2025-01-01T00:00:00Z"


class TestArchive:
    """Test Archive model."""

    def test_create_empty_archive(self):
        """Test creating an empty archive."""
        archive = Archive()
        assert archive.version == "1.0"
        assert archive.entries == []

    def test_archive_with_entries(self):
        """Test archive with entries."""
        f = ProtonMailFilter(name="Test", status=FilterStatus.ARCHIVED)
        entries = [ArchiveEntry(filter=f)]
        archive = Archive(entries=entries)
        assert len(archive.entries) == 1

    def test_archive_serialization_roundtrip(self):
        """Test archive serialization/deserialization."""
        f1 = ProtonMailFilter(name="F1", status=FilterStatus.ARCHIVED)
        f2 = ProtonMailFilter(name="F2", status=FilterStatus.DEPRECATED)
        archive = Archive(entries=[
            ArchiveEntry(filter=f1, archived_at="2025-01-01T00:00:00Z"),
            ArchiveEntry(filter=f2, archived_at="2025-01-02T00:00:00Z"),
        ])
        data = archive.model_dump()
        archive2 = Archive.model_validate(data)
        assert len(archive2.entries) == 2
        assert archive2.entries[0].filter.status == FilterStatus.ARCHIVED
        assert archive2.entries[1].filter.status == FilterStatus.DEPRECATED
