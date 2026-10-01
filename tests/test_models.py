"""Tests for Pydantic models in filter_models.py and backup_models.py."""

import pytest
from datetime import datetime

from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, ConsolidatedFilter,
    ConditionGroup, ConditionType, Operator, ActionType, LogicType, FilterStatus,
    ScrapeEvidence,
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
            parameters={"folder": "Work"}
        )
        assert action.type == ActionType.MOVE_TO
        assert action.parameters == {"folder": "Work"}

    def test_action_default_parameters(self):
        """Test that parameters defaults to empty dict."""
        action = FilterAction(type=ActionType.TRASH)
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
                FilterAction(type=ActionType.TRASH)
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
                FilterAction(type=ActionType.TRASH)
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
        assert ActionType.TRASH.value == "trash"
        # Old name kept as an alias; it means Trash, not a permanent delete
        assert ActionType.DELETE is ActionType.TRASH

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
            actions=[FilterAction(type=ActionType.TRASH)],
        )
        f_archived = ProtonMailFilter(
            name="Test",
            status=FilterStatus.ARCHIVED,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="x")],
            actions=[FilterAction(type=ActionType.TRASH)],
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
            actions=[FilterAction(type=ActionType.TRASH)],
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


class TestScrapeEvidence:
    """Test raw scrape evidence and completeness fields on ProtonMailFilter."""

    def test_defaults_are_empty(self):
        f = ProtonMailFilter(name="Old")
        assert f.raw is None
        assert f.scrape_issues == []
        assert f.is_complete is True

    def test_issues_make_filter_incomplete(self):
        f = ProtonMailFilter(name="X", scrape_issues=["unknown action row"])
        assert f.is_complete is False

    def test_evidence_roundtrip(self):
        f = ProtonMailFilter(
            name="X",
            raw=ScrapeEvidence(conditions_text="the sender", actions_text="Label as\nWork"),
            scrape_issues=["boom"],
        )
        f2 = ProtonMailFilter.model_validate(f.model_dump())
        assert f2.raw.actions_text == "Label as\nWork"
        assert f2.scrape_issues == ["boom"]

    def test_content_hash_ignores_evidence(self):
        """Evidence records how a filter was read, not what it does."""
        a = ProtonMailFilter(name="X")
        b = ProtonMailFilter(
            name="X", raw=ScrapeEvidence(actions_text="whatever"), scrape_issues=["x"],
        )
        assert a.content_hash == b.content_hash


class TestSieveFilterMarker:
    """A Sieve filter is a script, so it is marked and hashed by that script."""

    def test_is_sieve_derived_from_captured_script(self):
        """Backups written before is_sieve existed mark a Sieve filter only by its script."""
        f = ProtonMailFilter.model_validate({"name": "S", "raw": {"sieve_text": 'fileinto "X";'}})
        assert f.is_sieve is True

    def test_wizard_filter_is_not_sieve(self):
        f = ProtonMailFilter(name="W", raw=ScrapeEvidence(conditions_text="c", actions_text="a"))
        assert f.is_sieve is False

    def test_explicit_flag_wins(self):
        """An empty script still means the Sieve editor opened."""
        f = ProtonMailFilter(name="S", is_sieve=True, raw=ScrapeEvidence(sieve_text=""))
        assert f.is_sieve is True

    def test_flag_survives_roundtrip(self):
        f = ProtonMailFilter(name="S", is_sieve=True, raw=ScrapeEvidence(sieve_text="keep;"))
        assert ProtonMailFilter.model_validate(f.model_dump()).is_sieve is True

    def test_content_hash_covers_script(self):
        a = ProtonMailFilter(name="S", is_sieve=True, raw=ScrapeEvidence(sieve_text='fileinto "A";'))
        b = ProtonMailFilter(name="S", is_sieve=True, raw=ScrapeEvidence(sieve_text='fileinto "B";'))
        assert a.content_hash != b.content_hash

    def test_content_hash_of_wizard_filter_unchanged(self):
        """Only Sieve filters hash their script, so existing wizard hashes stay valid."""
        a = ProtonMailFilter(name="X")
        b = ProtonMailFilter(name="X", raw=ScrapeEvidence(sieve_text="", actions_text="a"))
        assert a.content_hash == b.content_hash


class TestLegacyActions:
    """Backups from older versions recorded Trash as "delete" and Spam/Inbox by label."""

    def test_backup_delete_action_reads_as_trash(self):
        f = ProtonMailFilter.model_validate({
            "name": "old", "conditions": [{"type": "sender", "operator": "is", "value": "a"}],
            "actions": [{"type": "delete", "parameters": {}}],
        })
        assert [a.type for a in f.actions] == [ActionType.TRASH]
        assert f.is_complete

    @pytest.mark.parametrize("old, new", [("Spam", "spam"), ("Inbox - Default", "inbox"), ("Work", "Work")])
    def test_backup_system_folder_targets(self, old, new):
        a = FilterAction.model_validate({"type": "move_to", "parameters": {"folder": old}})
        assert a.parameters == {"folder": new}

    def test_direct_action_construction_migrates(self):
        assert FilterAction(type="delete").type == ActionType.TRASH


class TestEmptyConditionValue:
    """An empty condition value matches every message, so it is quarantined."""

    @pytest.mark.parametrize("cond", [
        {"type": "subject", "operator": "contains", "value": ""},
        {"type": "subject", "operator": "contains", "value": "   \t"},
        {"type": "sender", "operator": "is"},
    ])
    def test_empty_value_flags_filter_incomplete(self, cond):
        f = ProtonMailFilter.model_validate({
            "name": "delete all?",
            "conditions": [cond, {"type": "sender", "operator": "is", "value": "a@x.com"}],
            "actions": [{"type": "trash"}],
        })
        assert not f.is_complete
        assert [c.value for c in f.conditions] == ["a@x.com"]
        assert "condition 1: empty value" in f.scrape_issues[0]

    def test_empty_value_condition_object_flagged(self):
        f = ProtonMailFilter(
            name="obj",
            conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value=" ")],
        )
        assert not f.is_complete
        assert f.conditions == []

    def test_attachment_condition_needs_no_value(self):
        f = ProtonMailFilter.model_validate({
            "name": "att", "conditions": [{"type": "attachments", "operator": "has", "value": ""}],
        })
        assert f.is_complete
        assert len(f.conditions) == 1


@pytest.mark.parametrize("cond", [
    {"type": "attachments", "operator": "contains", "value": "pdf"},
    {"type": "sender", "operator": "has", "value": "a@x.com"},
])
def test_operator_type_mismatch_is_quarantined(cond):
    f = ProtonMailFilter.model_validate({"name": "m", "conditions": [cond]})
    assert not f.is_complete
    assert f.conditions == []
    assert "does not apply" in f.scrape_issues[0]


class TestMultiValueConditions:
    """Several values are an explicit list; a single value is always one literal."""

    def test_values_list_keys(self):
        c = FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, values=["a", "b"])
        assert c.keys == ["a", "b"]
        assert c.value == ""

    def test_one_element_list_is_a_single_value(self):
        c = FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, values=["a"])
        assert (c.value, c.values) == ("a", [])

    def test_separators_in_value_are_literal(self):
        c = FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="Invoice, Receipt")
        assert c.keys == ["Invoice, Receipt"]

    def test_both_forms_refused_directly(self):
        with pytest.raises(ValueError):
            FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a", values=["b", "c"])

    def test_both_forms_in_backup_quarantined(self):
        f = ProtonMailFilter.model_validate({"name": "x", "conditions": [
            {"type": "sender", "operator": "is", "value": "a", "values": ["b", "c"]}]})
        assert not f.is_complete
        assert f.conditions == []

    def test_empty_entry_in_values_quarantined(self):
        f = ProtonMailFilter.model_validate({"name": "x", "conditions": [
            {"type": "sender", "operator": "is", "values": ["b", " "]}]})
        assert not f.is_complete

    def test_single_value_serializes_without_values_key(self):
        c = FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a")
        assert c.model_dump(mode="json") == {"type": "sender", "operator": "is", "value": "a"}

    def test_list_and_joined_literal_hash_differently(self):
        listed = ProtonMailFilter(name="f", conditions=[
            FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, values=["Invoice", "Receipt"])])
        literal = ProtonMailFilter(name="f", conditions=[
            FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="Invoice, Receipt")])
        assert listed.content_hash != literal.content_hash

    def test_single_value_hash_unchanged_by_values_field(self):
        """Existing filters keep their hash (archive and manifest keys)."""
        import hashlib
        f = ProtonMailFilter(name="f", conditions=[
            FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a")])
        expected = hashlib.sha256("name=f\nlogic=and\ncond:sender|is|a".encode()).hexdigest()[:16]
        assert f.content_hash == expected

    def test_round_trip_through_json(self):
        f = ProtonMailFilter(name="f", conditions=[
            FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, values=["a", "b"])])
        again = ProtonMailFilter.model_validate_json(f.model_dump_json())
        assert again.conditions[0].values == ["a", "b"]
        assert again.content_hash == f.content_hash

    OLD_CARRIED = {
        "name": "Carried forward (2026-01-01): sender is -> discard;",
        "conditions": [{"type": "sender", "operator": "is", "value": "a|b"}],
        "actions": [{"type": "delete"}],
    }

    def test_old_carried_filter_pipe_value_becomes_list(self):
        """An archive entry written before source_format existed (V4)."""
        from src.models.backup_models import ArchiveEntry
        entry = ArchiveEntry.model_validate({"filter": self.OLD_CARRIED})
        assert entry.filter.conditions[0].values == ["a", "b"]

    def test_stamped_carried_filter_pipe_value_is_literal(self):
        """V4: carry-forward today can store one key containing "|"."""
        from src.models.backup_models import ArchiveEntry, BACKUP_FORMAT_VERSION
        entry = ArchiveEntry.model_validate({"filter": self.OLD_CARRIED, "source_format": BACKUP_FORMAT_VERSION})
        assert entry.filter.conditions[0].keys == ["a|b"]

    def test_carried_name_outside_archive_is_literal(self):
        """V4: the legacy split never applies to a filter read on its own (scraped data)."""
        f = ProtonMailFilter.model_validate(self.OLD_CARRIED)
        assert f.conditions[0].keys == ["a|b"]

    def test_pipe_in_ordinary_filter_is_literal(self):
        f = ProtonMailFilter.model_validate({
            "name": "user filter",
            "conditions": [{"type": "subject", "operator": "contains", "value": "a|b"}],
        })
        assert f.conditions[0].keys == ["a|b"]
