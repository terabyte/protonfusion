"""Tests for diff engine."""

import pytest

from src.backup.diff_engine import DiffEngine, FilterDiff
from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction,
    ConditionType, Operator, ActionType, LogicType, FilterStatus,
)
from src.models.backup_models import Backup


class TestDiffEngine:
    """Test DiffEngine class."""

    def test_compare_empty_lists(self):
        """Test comparing two empty filter lists."""
        engine = DiffEngine()

        diff = engine.compare_filter_lists([], [])

        assert len(diff.added) == 0
        assert len(diff.removed) == 0
        assert len(diff.modified) == 0
        assert len(diff.state_changed) == 0
        assert len(diff.unchanged) == 0

    def test_compare_identical_lists(self, sample_filter_spam):
        """Test comparing identical filter lists."""
        engine = DiffEngine()

        diff = engine.compare_filter_lists([sample_filter_spam], [sample_filter_spam])

        assert len(diff.added) == 0
        assert len(diff.removed) == 0
        assert len(diff.modified) == 0
        assert len(diff.state_changed) == 0
        assert len(diff.unchanged) == 1

    def test_detect_added_filter(self, sample_filter_spam):
        """Test detecting added filters."""
        engine = DiffEngine()

        diff = engine.compare_filter_lists([], [sample_filter_spam])

        assert len(diff.added) == 1
        assert diff.added[0].name == "Spam Filter 1"
        assert len(diff.removed) == 0
        assert len(diff.modified) == 0

    def test_detect_removed_filter(self, sample_filter_spam):
        """Test detecting removed filters."""
        engine = DiffEngine()

        diff = engine.compare_filter_lists([sample_filter_spam], [])

        assert len(diff.removed) == 1
        assert diff.removed[0].name == "Spam Filter 1"
        assert len(diff.added) == 0
        assert len(diff.modified) == 0

    def test_detect_multiple_added(self, sample_filter_spam, sample_filter_move):
        """Test detecting multiple added filters."""
        engine = DiffEngine()

        diff = engine.compare_filter_lists([], [sample_filter_spam, sample_filter_move])

        assert len(diff.added) == 2

    def test_detect_multiple_removed(self, sample_filter_spam, sample_filter_move):
        """Test detecting multiple removed filters."""
        engine = DiffEngine()

        diff = engine.compare_filter_lists([sample_filter_spam, sample_filter_move], [])

        assert len(diff.removed) == 2

    def test_detect_state_changed(self):
        """Test detecting enabled/disabled state change."""
        engine = DiffEngine()

        old = ProtonMailFilter(
            name="Test",
            enabled=True,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )
        new = ProtonMailFilter(
            name="Test",
            enabled=False,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )

        diff = engine.compare_filter_lists([old], [new])

        assert len(diff.state_changed) == 1
        assert diff.state_changed[0][0].enabled is True
        assert diff.state_changed[0][1].enabled is False
        assert len(diff.modified) == 0

    def test_detect_modified_condition(self):
        """Test detecting modified filter conditions."""
        engine = DiffEngine()

        old = ProtonMailFilter(
            name="Test",
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="old")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )
        new = ProtonMailFilter(
            name="Test",
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="new")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )

        diff = engine.compare_filter_lists([old], [new])

        assert len(diff.modified) == 1
        assert diff.modified[0][0].conditions[0].value == "old"
        assert diff.modified[0][1].conditions[0].value == "new"

    def test_detect_modified_action(self):
        """Test detecting modified filter actions."""
        engine = DiffEngine()

        old = ProtonMailFilter(
            name="Test",
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test")],
            actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Old"})]
        )
        new = ProtonMailFilter(
            name="Test",
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test")],
            actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "New"})]
        )

        diff = engine.compare_filter_lists([old], [new])

        assert len(diff.modified) == 1
        assert diff.modified[0][0].actions[0].parameters["folder"] == "Old"
        assert diff.modified[0][1].actions[0].parameters["folder"] == "New"

    def test_detect_modified_priority(self):
        """Test detecting modified priority."""
        engine = DiffEngine()

        old = ProtonMailFilter(
            name="Test",
            priority=1,
            conditions=[],
            actions=[]
        )
        new = ProtonMailFilter(
            name="Test",
            priority=5,
            conditions=[],
            actions=[]
        )

        diff = engine.compare_filter_lists([old], [new])

        assert len(diff.modified) == 1

    def test_state_and_content_changed(self):
        """Test when both state and content change."""
        engine = DiffEngine()

        old = ProtonMailFilter(
            name="Test",
            enabled=True,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="old")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )
        new = ProtonMailFilter(
            name="Test",
            enabled=False,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="new")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )

        diff = engine.compare_filter_lists([old], [new])

        # Should be in modified, not state_changed, because content also changed
        assert len(diff.modified) == 1
        assert len(diff.state_changed) == 0

    def test_complex_diff(self, sample_filter_spam, sample_filter_move):
        """Test a complex diff with multiple types of changes."""
        engine = DiffEngine()

        # Create various changes
        unchanged = ProtonMailFilter(name="Unchanged", conditions=[], actions=[])
        removed = ProtonMailFilter(name="Removed", conditions=[], actions=[])
        old_modified = ProtonMailFilter(
            name="Modified",
            priority=1,
            conditions=[],
            actions=[]
        )
        new_modified = ProtonMailFilter(
            name="Modified",
            priority=5,
            conditions=[],
            actions=[]
        )
        added = ProtonMailFilter(name="Added", conditions=[], actions=[])

        old_list = [unchanged, removed, old_modified]
        new_list = [unchanged, new_modified, added]

        diff = engine.compare_filter_lists(old_list, new_list)

        assert len(diff.unchanged) == 1
        assert len(diff.removed) == 1
        assert len(diff.modified) == 1
        assert len(diff.added) == 1

    def test_compare_backups(self, sample_backup):
        """Test comparing two backups."""
        engine = DiffEngine()

        # Create two backups with different filters
        backup1 = sample_backup
        backup2 = Backup(
            filters=[
                ProtonMailFilter(name="New Filter", conditions=[], actions=[])
            ]
        )

        diff = engine.compare_backups(backup1, backup2)

        assert isinstance(diff, FilterDiff)
        # backup1 has 3 filters, backup2 has 1 new filter
        assert len(diff.removed) > 0 or len(diff.added) > 0

    def test_generate_summary_empty(self):
        """Test generating summary for empty diff."""
        engine = DiffEngine()
        diff = FilterDiff()

        summary = engine.generate_summary(diff)

        assert summary["added"] == 0
        assert summary["removed"] == 0
        assert summary["modified"] == 0
        assert summary["state_changed"] == 0
        assert summary["unchanged"] == 0
        assert summary["total_changes"] == 0

    def test_generate_summary_with_changes(self):
        """Test generating summary with changes."""
        engine = DiffEngine()

        old = ProtonMailFilter(name="Old", conditions=[], actions=[])
        new = ProtonMailFilter(name="New", conditions=[], actions=[])
        unchanged = ProtonMailFilter(name="Unchanged", conditions=[], actions=[])

        diff = FilterDiff(
            added=[new],
            removed=[old],
            modified=[],
            state_changed=[],
            unchanged=[unchanged]
        )

        summary = engine.generate_summary(diff)

        assert summary["added"] == 1
        assert summary["removed"] == 1
        assert summary["modified"] == 0
        assert summary["state_changed"] == 0
        assert summary["unchanged"] == 1
        assert summary["total_changes"] == 2

    def test_generate_summary_all_types(self):
        """Test summary with all types of changes."""
        engine = DiffEngine()

        f1 = ProtonMailFilter(name="F1", conditions=[], actions=[])
        f2 = ProtonMailFilter(name="F2", conditions=[], actions=[])
        f3 = ProtonMailFilter(name="F3", enabled=True, conditions=[], actions=[])
        f3_new = ProtonMailFilter(name="F3", enabled=False, conditions=[], actions=[])
        f4 = ProtonMailFilter(name="F4", priority=1, conditions=[], actions=[])
        f4_new = ProtonMailFilter(name="F4", priority=2, conditions=[], actions=[])
        f5 = ProtonMailFilter(name="F5", conditions=[], actions=[])

        diff = FilterDiff(
            added=[f1],
            removed=[f2],
            modified=[(f4, f4_new)],
            state_changed=[(f3, f3_new)],
            unchanged=[f5]
        )

        summary = engine.generate_summary(diff)

        assert summary["added"] == 1
        assert summary["removed"] == 1
        assert summary["modified"] == 1
        assert summary["state_changed"] == 1
        assert summary["unchanged"] == 1
        assert summary["total_changes"] == 4

    def test_filters_equal(self):
        """Test internal _filters_equal method."""
        engine = DiffEngine()

        f1 = ProtonMailFilter(
            name="Test",
            enabled=True,
            priority=1,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )
        f2 = ProtonMailFilter(
            name="Test",
            enabled=True,
            priority=1,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test")],
            actions=[FilterAction(type=ActionType.DELETE)]
        )

        assert engine._filters_equal(f1, f2)

    def test_filters_not_equal(self):
        """Test that different filters are not equal."""
        engine = DiffEngine()

        f1 = ProtonMailFilter(name="Test1", conditions=[], actions=[])
        f2 = ProtonMailFilter(name="Test2", conditions=[], actions=[])

        assert not engine._filters_equal(f1, f2)

    def test_filters_equal_except_enabled(self):
        """Test internal _filters_equal_except_enabled method."""
        engine = DiffEngine()

        f1 = ProtonMailFilter(
            name="Test",
            enabled=True,
            priority=1,
            conditions=[],
            actions=[]
        )
        f2 = ProtonMailFilter(
            name="Test",
            enabled=False,
            priority=1,
            conditions=[],
            actions=[]
        )

        assert engine._filters_equal_except_enabled(f1, f2)


class TestStatusAwareDiff:
    """Test diff engine with FilterStatus changes."""

    def test_status_change_detected(self):
        """Test that status change from enabled to archived is detected as state_changed."""
        engine = DiffEngine()
        old = ProtonMailFilter(name="Test", status=FilterStatus.ENABLED, conditions=[], actions=[])
        new = ProtonMailFilter(name="Test", status=FilterStatus.ARCHIVED, conditions=[], actions=[])

        diff = engine.compare_filter_lists([old], [new])
        assert len(diff.state_changed) == 1

    def test_same_status_no_change(self):
        """Test that same status is detected as unchanged."""
        engine = DiffEngine()
        f = ProtonMailFilter(name="Test", status=FilterStatus.ARCHIVED, conditions=[], actions=[])

        diff = engine.compare_filter_lists([f], [f])
        assert len(diff.unchanged) == 1

    def test_status_excluded_from_equality(self):
        """Test that status field is excluded from content equality."""
        engine = DiffEngine()
        f1 = ProtonMailFilter(name="Test", status=FilterStatus.ENABLED, conditions=[], actions=[])
        f2 = ProtonMailFilter(name="Test", status=FilterStatus.ENABLED, conditions=[], actions=[])

        assert engine._filters_equal(f1, f2)

    def test_filters_equal_ignores_status_difference(self):
        """_filters_equal should ignore differing status (it checks content only)."""
        engine = DiffEngine()
        f1 = ProtonMailFilter(name="Test", status=FilterStatus.ENABLED, priority=1, conditions=[], actions=[])
        f2 = ProtonMailFilter(name="Test", status=FilterStatus.ARCHIVED, priority=1, conditions=[], actions=[])

        # _filters_equal excludes status, but enabled differs
        # enabled is True for ENABLED, False for ARCHIVED
        # So they're not model_dump-equal due to enabled field
        assert not engine._filters_equal(f1, f2)
        # But they are equal except enabled/status
        assert engine._filters_equal_except_enabled(f1, f2)
