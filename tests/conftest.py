"""Shared pytest fixtures for ProtonFusion tests."""

import json
import pytest
from datetime import datetime
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption(
        "--credentials-file",
        action="store",
        default=None,
        help="Path to ProtonMail credentials file for e2e tests",
    )

from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, ConsolidatedFilter,
    ConditionGroup, ConditionType, Operator, ActionType, LogicType, FilterStatus,
)
from src.models.backup_models import Backup, BackupMetadata, ArchiveEntry, Archive


@pytest.fixture
def sample_condition_sender():
    """Sample sender condition."""
    return FilterCondition(
        type=ConditionType.SENDER,
        operator=Operator.CONTAINS,
        value="spam@example.com"
    )


@pytest.fixture
def sample_condition_subject():
    """Sample subject condition."""
    return FilterCondition(
        type=ConditionType.SUBJECT,
        operator=Operator.CONTAINS,
        value="urgent"
    )


@pytest.fixture
def sample_condition_recipient():
    """Sample recipient condition."""
    return FilterCondition(
        type=ConditionType.RECIPIENT,
        operator=Operator.IS,
        value="me@example.com"
    )


@pytest.fixture
def sample_action_delete():
    """Sample delete action."""
    return FilterAction(
        type=ActionType.DELETE,
        parameters={}
    )


@pytest.fixture
def sample_action_move():
    """Sample move action."""
    return FilterAction(
        type=ActionType.MOVE_TO,
        parameters={"folder": "Spam"}
    )


@pytest.fixture
def sample_action_label():
    """Sample label action."""
    return FilterAction(
        type=ActionType.LABEL,
        parameters={"label": "Important"}
    )


@pytest.fixture
def sample_filter_spam(sample_condition_sender, sample_action_delete):
    """Sample spam filter."""
    return ProtonMailFilter(
        name="Spam Filter 1",
        enabled=True,
        priority=0,
        logic=LogicType.AND,
        conditions=[sample_condition_sender],
        actions=[sample_action_delete]
    )


@pytest.fixture
def sample_filter_move(sample_condition_subject, sample_action_move):
    """Sample filter that moves to folder."""
    return ProtonMailFilter(
        name="Move to Spam",
        enabled=True,
        priority=1,
        logic=LogicType.AND,
        conditions=[sample_condition_subject],
        actions=[sample_action_move]
    )


@pytest.fixture
def sample_filter_disabled(sample_condition_recipient, sample_action_label):
    """Sample disabled filter."""
    return ProtonMailFilter(
        name="Disabled Filter",
        enabled=False,
        priority=5,
        logic=LogicType.OR,
        conditions=[sample_condition_recipient],
        actions=[sample_action_label]
    )


@pytest.fixture
def sample_filter_complex():
    """Complex filter with multiple conditions and actions."""
    return ProtonMailFilter(
        name="Complex Filter",
        enabled=True,
        priority=2,
        logic=LogicType.AND,
        conditions=[
            FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="newsletter"),
            FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="subscribe"),
        ],
        actions=[
            FilterAction(type=ActionType.LABEL, parameters={"label": "Newsletters"}),
            FilterAction(type=ActionType.MARK_READ, parameters={}),
        ]
    )


@pytest.fixture
def sample_filters_list(sample_filter_spam, sample_filter_move, sample_filter_disabled):
    """List of sample filters."""
    return [sample_filter_spam, sample_filter_move, sample_filter_disabled]


@pytest.fixture
def sample_consolidated_filter():
    """Sample consolidated filter."""
    return ConsolidatedFilter(
        name="Delete spam (consolidated from 3 filters)",
        condition_groups=[
            ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1@test.com")]),
            ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2@test.com")]),
            ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam3@test.com")]),
        ],
        actions=[
            FilterAction(type=ActionType.DELETE, parameters={}),
        ],
        source_filters=["Spam Filter 1", "Spam Filter 2", "Spam Filter 3"],
        filter_count=3
    )


@pytest.fixture
def sample_backup_metadata():
    """Sample backup metadata."""
    return BackupMetadata(
        filter_count=5,
        enabled_count=4,
        disabled_count=1,
        account_email="test@proton.me",
        tool_version="0.1.0"
    )


@pytest.fixture
def sample_backup(sample_filters_list, sample_backup_metadata):
    """Sample backup object."""
    return Backup(
        version="1.0",
        timestamp=datetime(2025, 1, 15, 10, 30, 0),
        metadata=sample_backup_metadata,
        filters=sample_filters_list,
        checksum="sha256:abc123def456"
    )


@pytest.fixture
def raw_filter_dict():
    """Raw filter dictionary as scraped from ProtonMail."""
    return {
        "name": "Test Filter",
        "enabled": True,
        "priority": 1,
        "logic": "and",
        "conditions": [
            {
                "type": "sender",
                "operator": "contains",
                "value": "test@example.com"
            }
        ],
        "actions": [
            {
                "type": "move to",
                "parameters": {"folder": "Test Folder"}
            }
        ]
    }


@pytest.fixture
def raw_filters_list(raw_filter_dict):
    """List of raw filter dictionaries."""
    return [
        raw_filter_dict,
        {
            "name": "Another Filter",
            "enabled": False,
            "priority": 2,
            "logic": "or",
            "conditions": [
                {"type": "subject", "operator": "is", "value": "Test"}
            ],
            "actions": [
                {"type": "label", "parameters": {"label": "Important"}}
            ]
        }
    ]


@pytest.fixture
def temp_snapshots_dir(tmp_path):
    """Temporary directory for snapshots in tests."""
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    return snapshots_dir


@pytest.fixture
def temp_credentials_file(tmp_path):
    """Temporary credentials file."""
    creds_file = tmp_path / ".credentials"
    creds_file.write_text("Username: test@proton.me\nPassword: testpass123\n")
    return creds_file


@pytest.fixture
def sample_filter_archived(sample_condition_sender, sample_action_delete):
    """Sample archived filter."""
    return ProtonMailFilter(
        name="Archived Filter",
        status=FilterStatus.ARCHIVED,
        priority=0,
        logic=LogicType.AND,
        conditions=[sample_condition_sender],
        actions=[sample_action_delete]
    )


@pytest.fixture
def sample_filter_deprecated(sample_condition_subject, sample_action_move):
    """Sample deprecated filter."""
    return ProtonMailFilter(
        name="Deprecated Filter",
        status=FilterStatus.DEPRECATED,
        priority=0,
        logic=LogicType.AND,
        conditions=[sample_condition_subject],
        actions=[sample_action_move]
    )


@pytest.fixture
def sample_archive_entry(sample_filter_archived):
    """Sample archive entry."""
    return ArchiveEntry(
        filter=sample_filter_archived,
        archived_at="2025-06-15T10:00:00+00:00",
        source_snapshot="2025-06-15_10-00-00",
    )


@pytest.fixture
def sample_archive_entries(sample_filter_archived, sample_filter_deprecated):
    """List of sample archive entries with mixed statuses."""
    return [
        ArchiveEntry(
            filter=sample_filter_archived,
            archived_at="2025-06-15T10:00:00+00:00",
            source_snapshot="2025-06-15_10-00-00",
        ),
        ArchiveEntry(
            filter=sample_filter_deprecated,
            archived_at="2025-06-15T10:00:00+00:00",
            source_snapshot="2025-06-15_10-00-00",
        ),
    ]


@pytest.fixture
def temp_snapshot_with_archive(temp_snapshots_dir, sample_filters_list, sample_archive_entries):
    """Pre-populated snapshot directory with backup.json and archive.json."""
    from src.backup.backup_manager import BackupManager

    manager = BackupManager(temp_snapshots_dir)
    manager.create_backup(sample_filters_list, "test@proton.me")
    snapshot_dir = manager.snapshot_dir_for("latest")
    manager.write_archive(snapshot_dir, sample_archive_entries)
    return snapshot_dir, manager, sample_archive_entries
