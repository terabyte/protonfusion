"""Tests for consolidation engine and strategies."""

import pytest

from src.consolidator.consolidation_engine import ConsolidationEngine, ConsolidationReport
from src.consolidator.strategies.group_by_action import group_by_action
from src.consolidator.strategies.merge_conditions import merge_conditions
from src.consolidator.strategies.optimize_ordering import optimize_ordering
from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, ConsolidatedFilter,
    ConditionGroup, ConditionType, Operator, ActionType, LogicType, FilterStatus,
)


class TestGroupByAction:
    """Test group_by_action strategy."""

    def test_group_empty_list(self):
        """Test grouping empty filter list."""
        result = group_by_action([])
        assert result == []

    def test_single_filter_no_grouping(self, sample_filter_spam):
        """Test that single filter is not grouped."""
        result = group_by_action([sample_filter_spam])

        assert len(result) == 1
        assert result[0].filter_count == 1

    def test_group_filters_same_action(self):
        """Test grouping filters with same action."""
        filters = [
            ProtonMailFilter(
                name="Spam 1",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Spam 2",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Spam 3",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam3")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        assert result[0].filter_count == 3
        assert len(result[0].condition_groups) == 3

    def test_group_filters_different_actions(self):
        """Test that filters with different actions are not grouped."""
        filters = [
            ProtonMailFilter(
                name="Delete",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Archive",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="news")],
                actions=[FilterAction(type=ActionType.ARCHIVE)]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 2

    def test_processes_all_filters_passed_to_it(self):
        """Test that the strategy processes all filters it receives (filtering is the engine's job)."""
        filters = [
            ProtonMailFilter(
                name="Enabled",
                enabled=True,
                conditions=[],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Disabled",
                enabled=False,
                conditions=[],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        assert set(result[0].source_filters) == {"Enabled", "Disabled"}

    def test_group_with_same_folder(self):
        """Test grouping filters with same folder destination."""
        filters = [
            ProtonMailFilter(
                name="Move 1",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="news1")],
                actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "News"})]
            ),
            ProtonMailFilter(
                name="Move 2",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="news2")],
                actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "News"})]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        assert result[0].filter_count == 2

    def test_different_folders_not_grouped(self):
        """Test that filters moving to different folders are not grouped."""
        filters = [
            ProtonMailFilter(
                name="Move 1",
                conditions=[],
                actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "News"})]
            ),
            ProtonMailFilter(
                name="Move 2",
                conditions=[],
                actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Spam"})]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 2

    def test_source_filters_tracked(self):
        """Test that source filter names are tracked."""
        filters = [
            ProtonMailFilter(name="Filter A", conditions=[], actions=[FilterAction(type=ActionType.DELETE)]),
            ProtonMailFilter(name="Filter B", conditions=[], actions=[FilterAction(type=ActionType.DELETE)]),
            ProtonMailFilter(name="Filter C", conditions=[], actions=[FilterAction(type=ActionType.DELETE)]),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        assert set(result[0].source_filters) == {"Filter A", "Filter B", "Filter C"}

    def test_consolidated_name_generated(self):
        """Test that consolidated filter gets descriptive name."""
        filters = [
            ProtonMailFilter(name="F1", conditions=[], actions=[FilterAction(type=ActionType.DELETE)]),
            ProtonMailFilter(name="F2", conditions=[], actions=[FilterAction(type=ActionType.DELETE)]),
        ]

        result = group_by_action(filters)

        assert "Delete" in result[0].name
        assert "consolidated from 2 filters" in result[0].name

    def test_preserves_and_logic_in_condition_group(self):
        """Test that a filter with AND logic keeps its conditions as an AND group."""
        filters = [
            ProtonMailFilter(
                name="AND filter",
                logic=LogicType.AND,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        assert len(result[0].condition_groups) == 1
        group = result[0].condition_groups[0]
        assert group.logic == LogicType.AND
        assert len(group.conditions) == 2

    def test_preserves_or_logic_in_condition_group(self):
        """Test that a filter with OR logic keeps its conditions as an OR group."""
        filters = [
            ProtonMailFilter(
                name="OR filter",
                logic=LogicType.OR,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        group = result[0].condition_groups[0]
        assert group.logic == LogicType.OR
        assert len(group.conditions) == 2

    def test_and_filter_not_flattened_with_single_condition_filter(self):
        """Test the critical bug fix: AND filter conditions are NOT flattened
        into an OR with single-condition filters.

        Original behavior (WRONG):
          Filter A: sender=alice AND subject=urgent -> delete
          Filter B: sender=bob -> delete
          Consolidated: sender=alice OR subject=urgent OR sender=bob -> delete
          (This would delete emails from alice even without "urgent" subject!)

        Correct behavior:
          Consolidated: (sender=alice AND subject=urgent) OR (sender=bob) -> delete
        """
        filters = [
            ProtonMailFilter(
                name="Multi-condition AND",
                logic=LogicType.AND,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Single condition",
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="bob"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        cf = result[0]
        # Must have 2 separate condition groups, not flat conditions
        assert len(cf.condition_groups) == 2

        # First group: AND with 2 conditions
        and_group = cf.condition_groups[0]
        assert and_group.logic == LogicType.AND
        assert len(and_group.conditions) == 2
        values = {c.value for c in and_group.conditions}
        assert values == {"alice", "urgent"}

        # Second group: single condition
        single_group = cf.condition_groups[1]
        assert len(single_group.conditions) == 1
        assert single_group.conditions[0].value == "bob"

    def test_multiple_and_filters_stay_separate(self):
        """Test that multiple AND filters each become their own group."""
        filters = [
            ProtonMailFilter(
                name="Filter A",
                logic=LogicType.AND,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Filter B",
                logic=LogicType.AND,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="bob"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="sale"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]

        result = group_by_action(filters)

        assert len(result) == 1
        cf = result[0]
        assert len(cf.condition_groups) == 2
        assert cf.condition_groups[0].logic == LogicType.AND
        assert cf.condition_groups[1].logic == LogicType.AND


class TestMergeConditions:
    """Test merge_conditions strategy."""

    def test_merge_empty_list(self):
        """Test merging empty list."""
        result = merge_conditions([])
        assert result == []

    def test_single_group_not_merged(self):
        """Test that single condition group is not merged."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test")])
            ],
            actions=[]
        )

        result = merge_conditions([cf])

        assert len(result) == 1
        assert len(result[0].condition_groups) == 1

    def test_merge_compatible_single_condition_groups(self):
        """Test merging single-condition groups with same type and operator."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1")]),
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2")]),
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam3")]),
            ],
            actions=[]
        )

        result = merge_conditions([cf])

        assert len(result) == 1
        # 3 single-condition groups should merge into 1
        assert len(result[0].condition_groups) == 1
        merged_value = result[0].condition_groups[0].conditions[0].value
        assert "spam1" in merged_value
        assert "spam2" in merged_value
        assert "spam3" in merged_value
        assert "|" in merged_value

    def test_different_types_not_merged(self):
        """Test that single-condition groups with different types are not merged."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test1")]),
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="test2")]),
            ],
            actions=[]
        )

        result = merge_conditions([cf])

        assert len(result[0].condition_groups) == 2

    def test_different_operators_not_merged(self):
        """Test that single-condition groups with different operators are not merged."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="test1")]),
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="test2")]),
            ],
            actions=[]
        )

        result = merge_conditions([cf])

        assert len(result[0].condition_groups) == 2

    def test_merge_preserves_other_fields(self):
        """Test that merging preserves other filter fields."""
        cf = ConsolidatedFilter(
            name="Original Name",
            condition_groups=[
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="a")]),
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="b")]),
            ],
            actions=[FilterAction(type=ActionType.DELETE)],
            source_filters=["A", "B"],
            filter_count=2
        )

        result = merge_conditions([cf])

        assert result[0].name == "Original Name"
        assert len(result[0].actions) == 1
        assert result[0].filter_count == 2

    def test_multi_condition_groups_not_merged(self):
        """Test that multi-condition AND groups are never merged with others."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[
                # Multi-condition AND group - must be preserved as-is
                ConditionGroup(
                    logic=LogicType.AND,
                    conditions=[
                        FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                        FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                    ]
                ),
                # Single-condition group
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="bob")]),
            ],
            actions=[FilterAction(type=ActionType.DELETE)],
        )

        result = merge_conditions([cf])

        # Both groups should be preserved (multi-condition can't merge with single)
        assert len(result[0].condition_groups) == 2

        # Find the multi-condition group - verify it's intact
        multi = [g for g in result[0].condition_groups if len(g.conditions) == 2]
        assert len(multi) == 1
        assert multi[0].logic == LogicType.AND
        assert {c.value for c in multi[0].conditions} == {"alice", "urgent"}

    def test_merge_single_groups_alongside_multi_groups(self):
        """Test that compatible single-condition groups merge while
        multi-condition groups are preserved separately."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[
                # These two single-condition groups can merge
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1")]),
                ConditionGroup(conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2")]),
                # This multi-condition group must stay separate
                ConditionGroup(
                    logic=LogicType.AND,
                    conditions=[
                        FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                        FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                    ]
                ),
            ],
            actions=[FilterAction(type=ActionType.DELETE)],
        )

        result = merge_conditions([cf])

        # Should be 2 groups: 1 merged single + 1 preserved multi
        assert len(result[0].condition_groups) == 2

        singles = [g for g in result[0].condition_groups if len(g.conditions) == 1]
        multis = [g for g in result[0].condition_groups if len(g.conditions) == 2]
        assert len(singles) == 1
        assert len(multis) == 1
        assert "|" in singles[0].conditions[0].value  # merged
        assert multis[0].logic == LogicType.AND


class TestOptimizeOrdering:
    """Test optimize_ordering strategy."""

    def test_optimize_empty_list(self):
        """Test optimizing empty list."""
        result = optimize_ordering([])
        assert result == []

    def test_single_filter_unchanged(self):
        """Test single filter ordering."""
        cf = ConsolidatedFilter(
            name="Test",
            condition_groups=[],
            actions=[FilterAction(type=ActionType.DELETE)]
        )

        result = optimize_ordering([cf])

        assert len(result) == 1

    def test_delete_comes_first(self):
        """Test that delete actions come first."""
        filters = [
            ConsolidatedFilter(name="Move", actions=[FilterAction(type=ActionType.MOVE_TO)], filter_count=1),
            ConsolidatedFilter(name="Delete", actions=[FilterAction(type=ActionType.DELETE)], filter_count=1),
            ConsolidatedFilter(name="Label", actions=[FilterAction(type=ActionType.LABEL)], filter_count=1),
        ]

        result = optimize_ordering(filters)

        assert result[0].name == "Delete"

    def test_priority_ordering(self):
        """Test that filters are ordered by action priority."""
        filters = [
            ConsolidatedFilter(name="Star", actions=[FilterAction(type=ActionType.STAR)], filter_count=1),
            ConsolidatedFilter(name="Delete", actions=[FilterAction(type=ActionType.DELETE)], filter_count=1),
            ConsolidatedFilter(name="Archive", actions=[FilterAction(type=ActionType.ARCHIVE)], filter_count=1),
            ConsolidatedFilter(name="Move", actions=[FilterAction(type=ActionType.MOVE_TO)], filter_count=1),
            ConsolidatedFilter(name="Label", actions=[FilterAction(type=ActionType.LABEL)], filter_count=1),
            ConsolidatedFilter(name="Mark Read", actions=[FilterAction(type=ActionType.MARK_READ)], filter_count=1),
        ]

        result = optimize_ordering(filters)

        # Delete should be first, Star should be last
        assert result[0].name == "Delete"
        assert result[-1].name in ["Star", "Mark Read"]

    def test_filter_count_secondary_sort(self):
        """Test that higher filter_count comes first within same action type."""
        filters = [
            ConsolidatedFilter(name="Delete 1", actions=[FilterAction(type=ActionType.DELETE)], filter_count=1),
            ConsolidatedFilter(name="Delete 5", actions=[FilterAction(type=ActionType.DELETE)], filter_count=5),
            ConsolidatedFilter(name="Delete 3", actions=[FilterAction(type=ActionType.DELETE)], filter_count=3),
        ]

        result = optimize_ordering(filters)

        assert result[0].filter_count == 5
        assert result[1].filter_count == 3
        assert result[2].filter_count == 1


class TestConsolidationEngine:
    """Test ConsolidationEngine class."""

    def test_consolidate_empty_list(self):
        """Test consolidating empty filter list."""
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate([])

        assert consolidated == []
        assert report.original_count == 0
        assert report.consolidated_count == 0

    def test_consolidate_basic(self):
        """Test basic consolidation."""
        filters = [
            ProtonMailFilter(
                name="Spam 1",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Spam 2",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        assert len(consolidated) < len(filters)
        assert report.original_count == 2
        assert report.enabled_count == 2

    def test_consolidate_skips_disabled(self):
        """Test that consolidation skips disabled filters."""
        filters = [
            ProtonMailFilter(name="Enabled", enabled=True, conditions=[], actions=[]),
            ProtonMailFilter(name="Disabled", enabled=False, conditions=[], actions=[]),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        assert report.original_count == 2
        assert report.enabled_count == 1
        assert report.disabled_skipped == 1

    def test_consolidate_generates_report(self):
        """Test that consolidation generates a report."""
        filters = [
            ProtonMailFilter(
                name=f"Filter {i}",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value=f"test{i}")],
                actions=[FilterAction(type=ActionType.DELETE)]
            )
            for i in range(5)
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        assert isinstance(report, ConsolidationReport)
        assert report.original_count == 5
        assert report.enabled_count == 5
        assert report.consolidated_count > 0
        assert report.reduction_percent > 0

    def test_consolidate_reduction_percent(self):
        """Test reduction percentage calculation."""
        filters = [
            ProtonMailFilter(
                name=f"Spam {i}",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value=f"spam{i}")],
                actions=[FilterAction(type=ActionType.DELETE)]
            )
            for i in range(10)
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        # 10 filters should consolidate to 1
        assert len(consolidated) == 1
        assert report.reduction_percent > 80  # Should be 90%

    def test_analyze_filters(self):
        """Test analyze method."""
        filters = [
            ProtonMailFilter(
                name="Delete 1",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Delete 2",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="ad")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Disabled",
                enabled=False,
                conditions=[],
                actions=[]
            ),
        ]
        engine = ConsolidationEngine()

        analysis = engine.analyze(filters)

        assert analysis["total_filters"] == 3
        assert analysis["enabled"] == 2
        assert analysis["disabled"] == 1
        assert "action_distribution" in analysis
        assert "condition_distribution" in analysis
        assert "consolidation_opportunities" in analysis

    def test_analyze_identifies_opportunities(self):
        """Test that analyze identifies consolidation opportunities."""
        filters = [
            ProtonMailFilter(
                name=f"Spam {i}",
                enabled=True,
                conditions=[],
                actions=[FilterAction(type=ActionType.DELETE)]
            )
            for i in range(5)
        ]
        engine = ConsolidationEngine()

        analysis = engine.analyze(filters)

        # Should identify that 5 delete actions can be consolidated
        assert "consolidation_opportunities" in analysis
        assert len(analysis["consolidation_opportunities"]) > 0

    def test_full_consolidation_pipeline(self):
        """Test complete consolidation pipeline with all strategies."""
        filters = [
            # Group 1: Delete spam senders
            ProtonMailFilter(
                name="Spam 1",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1@test.com")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Spam 2",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2@test.com")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            # Group 2: Move newsletters
            ProtonMailFilter(
                name="Newsletter 1",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="newsletter")],
                actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "News"})]
            ),
            ProtonMailFilter(
                name="Newsletter 2",
                enabled=True,
                conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="updates")],
                actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "News"})]
            ),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        # Should consolidate 4 filters into 2
        assert len(consolidated) == 2
        assert report.original_count == 4
        assert report.consolidated_count == 2
        # Delete should come first (higher priority)
        assert consolidated[0].actions[0].type == ActionType.DELETE

    def test_pipeline_preserves_and_logic(self):
        """Test that the full pipeline preserves AND logic through all strategies."""
        filters = [
            ProtonMailFilter(
                name="AND filter",
                logic=LogicType.AND,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Simple filter",
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="bob"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        assert len(consolidated) == 1
        cf = consolidated[0]

        # The AND group must survive as a separate multi-condition group
        multi_groups = [g for g in cf.condition_groups if len(g.conditions) == 2]
        assert len(multi_groups) == 1
        assert multi_groups[0].logic == LogicType.AND

    def test_pipeline_mixed_and_or_single(self):
        """Test pipeline with a mix of AND, OR, and single-condition filters."""
        filters = [
            # AND filter: must stay as allof
            ProtonMailFilter(
                name="AND filter",
                logic=LogicType.AND,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="alice"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="urgent"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            # OR filter: must stay as anyof
            ProtonMailFilter(
                name="OR filter",
                logic=LogicType.OR,
                conditions=[
                    FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="charlie"),
                    FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="sale"),
                ],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            # Two single-condition filters that CAN be merged
            ProtonMailFilter(
                name="Single 1",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam1")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
            ProtonMailFilter(
                name="Single 2",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam2")],
                actions=[FilterAction(type=ActionType.DELETE)]
            ),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        assert len(consolidated) == 1
        cf = consolidated[0]

        # Should have: 1 merged single-condition group + 1 AND group + 1 OR group = 3 groups
        assert len(cf.condition_groups) == 3

        and_groups = [g for g in cf.condition_groups if g.logic == LogicType.AND and len(g.conditions) == 2]
        or_groups = [g for g in cf.condition_groups if g.logic == LogicType.OR and len(g.conditions) == 2]
        merged_singles = [g for g in cf.condition_groups if len(g.conditions) == 1 and "|" in g.conditions[0].value]

        assert len(and_groups) == 1
        assert len(or_groups) == 1
        assert len(merged_singles) == 1
        assert "spam1" in merged_singles[0].conditions[0].value
        assert "spam2" in merged_singles[0].conditions[0].value


class TestDisabledFilterHandling:
    """Test include_disabled and synced_filter_hashes parameters."""

    def _make_filter(self, name, enabled=True, action_type=ActionType.DELETE):
        return ProtonMailFilter(
            name=name,
            enabled=enabled,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value=f"{name}@test.com")],
            actions=[FilterAction(type=action_type)],
        )

    def test_disabled_skipped_by_default(self):
        """Default behavior: disabled filters are skipped."""
        filters = [
            self._make_filter("Enabled1"),
            self._make_filter("Enabled2"),
            self._make_filter("Disabled1", enabled=False),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        assert report.original_count == 3
        assert report.enabled_count == 2
        assert report.disabled_skipped == 1
        assert report.disabled_included == 0
        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "Disabled1" not in all_sources

    def test_include_disabled_processes_all(self):
        """With include_disabled=True, all filters are processed."""
        filters = [
            self._make_filter("Enabled1"),
            self._make_filter("Disabled1", enabled=False),
            self._make_filter("Disabled2", enabled=False),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters, include_disabled=True)

        assert report.enabled_count == 3  # all selected
        assert report.disabled_skipped == 0
        assert report.disabled_included == 2
        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "Enabled1" in all_sources
        assert "Disabled1" in all_sources
        assert "Disabled2" in all_sources

    def test_synced_hashes_includes_matching_disabled(self):
        """Disabled filters whose content hashes match the synced set are included."""
        synced_filter = self._make_filter("SyncedDisabled", enabled=False)
        filters = [
            self._make_filter("Enabled1"),
            synced_filter,
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            filters, synced_filter_hashes={synced_filter.content_hash}
        )

        assert report.enabled_count == 2  # both selected
        assert report.disabled_skipped == 0
        assert report.disabled_included == 1
        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "SyncedDisabled" in all_sources

    def test_synced_hashes_ignores_non_matching_disabled(self):
        """Disabled filters not in the synced set are still skipped."""
        synced_filter = self._make_filter("SyncedDisabled", enabled=False)
        filters = [
            self._make_filter("Enabled1"),
            synced_filter,
            self._make_filter("UserDisabled", enabled=False),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            filters, synced_filter_hashes={synced_filter.content_hash}
        )

        assert report.enabled_count == 2
        assert report.disabled_skipped == 1
        assert report.disabled_included == 1
        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "SyncedDisabled" in all_sources
        assert "UserDisabled" not in all_sources

    def test_synced_hashes_plus_new_enabled(self):
        """Repeat-use scenario: synced disabled + new enabled all processed."""
        old_filters = [
            self._make_filter("Old1", enabled=False),
            self._make_filter("Old2", enabled=False),
            self._make_filter("Old3", enabled=False),
        ]
        filters = old_filters + [
            self._make_filter("New1", enabled=True),
            self._make_filter("New2", enabled=True),
        ]
        synced_hashes = {f.content_hash for f in old_filters}
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            filters, synced_filter_hashes=synced_hashes
        )

        assert report.enabled_count == 5  # all 5 selected
        assert report.disabled_skipped == 0
        assert report.disabled_included == 3
        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert set(all_sources) == {"Old1", "Old2", "Old3", "New1", "New2"}

    def test_include_disabled_overrides_synced_hashes(self):
        """include_disabled=True includes everything regardless of synced set."""
        disabled1 = self._make_filter("Disabled1", enabled=False)
        filters = [
            self._make_filter("Enabled1"),
            disabled1,
            self._make_filter("Disabled2", enabled=False),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            filters, include_disabled=True, synced_filter_hashes={disabled1.content_hash}
        )

        assert report.enabled_count == 3
        assert report.disabled_skipped == 0
        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "Disabled2" in all_sources

    def test_analyze_respects_include_disabled(self):
        """Analyze method also respects include_disabled flag."""
        filters = [
            self._make_filter("Enabled1"),
            self._make_filter("Disabled1", enabled=False),
        ]
        engine = ConsolidationEngine()

        stats = engine.analyze(filters, include_disabled=True)

        assert stats["enabled"] == 2
        assert stats["disabled"] == 0

    def test_analyze_respects_synced_filter_hashes(self):
        """Analyze method also respects synced_filter_hashes."""
        synced_filter = self._make_filter("SyncedDisabled", enabled=False)
        filters = [
            self._make_filter("Enabled1"),
            synced_filter,
            self._make_filter("UserDisabled", enabled=False),
        ]
        engine = ConsolidationEngine()

        stats = engine.analyze(filters, synced_filter_hashes={synced_filter.content_hash})

        assert stats["enabled"] == 2
        assert stats["disabled"] == 1
        assert stats["disabled_included"] == 1

    def test_same_name_different_content_distinguished(self):
        """Two filters with the same name but different conditions have different hashes."""
        filter_a = ProtonMailFilter(
            name="Newsletter",
            enabled=False,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="news@a.com")],
            actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "News"})],
        )
        filter_b = ProtonMailFilter(
            name="Newsletter",
            enabled=False,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="news@b.com")],
            actions=[FilterAction(type=ActionType.MARK_READ)],
        )

        assert filter_a.content_hash != filter_b.content_hash

        # Only filter_a was synced — filter_b should be skipped
        filters = [filter_a, filter_b]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            filters, synced_filter_hashes={filter_a.content_hash}
        )

        assert report.disabled_included == 1
        assert report.disabled_skipped == 1
        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        # filter_a included, filter_b skipped (both named "Newsletter")
        assert all_sources.count("Newsletter") == 1

    def test_edited_filter_not_matched(self):
        """A filter that was synced then edited by the user gets a new hash and is not auto-included."""
        original = ProtonMailFilter(
            name="Block spam",
            enabled=False,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam@old.com")],
            actions=[FilterAction(type=ActionType.DELETE)],
        )
        edited = ProtonMailFilter(
            name="Block spam",
            enabled=False,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="spam@new.com")],
            actions=[FilterAction(type=ActionType.DELETE)],
        )
        synced_hashes = {original.content_hash}

        engine = ConsolidationEngine()
        consolidated, report = engine.consolidate(
            [edited], synced_filter_hashes=synced_hashes
        )

        # Edited filter has different hash, should NOT be auto-included
        assert report.disabled_skipped == 1
        assert report.disabled_included == 0


class TestStatusBasedSelection:
    """Test four-status filter selection in consolidation engine."""

    def _make_filter(self, name, status=FilterStatus.ENABLED, action_type=ActionType.DELETE):
        return ProtonMailFilter(
            name=name,
            status=status,
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value=f"{name}@test.com")],
            actions=[FilterAction(type=action_type)],
        )

    def test_deprecated_always_excluded(self):
        """DEPRECATED filters are always excluded."""
        filters = [
            self._make_filter("Active"),
            self._make_filter("Dead", status=FilterStatus.DEPRECATED),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(filters)

        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "Active" in all_sources
        assert "Dead" not in all_sources

    def test_archived_always_included_via_param(self):
        """ARCHIVED filters passed via archived_filters param are always included."""
        backup_filters = [self._make_filter("FromBackup")]
        archived_filters = [self._make_filter("FromArchive", status=FilterStatus.ARCHIVED)]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            backup_filters, archived_filters=archived_filters,
        )

        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "FromBackup" in all_sources
        assert "FromArchive" in all_sources
        assert report.archived_count == 1

    def test_exclude_names_works(self):
        """Filters in exclude_names are excluded."""
        filters = [
            self._make_filter("Keep"),
            self._make_filter("Skip"),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            filters, exclude_names={"Skip"},
        )

        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "Keep" in all_sources
        assert "Skip" not in all_sources
        assert report.excluded_count == 1

    def test_exclude_names_applies_to_archived(self):
        """Exclude names also apply to archived filters."""
        archived_filters = [
            self._make_filter("ArchiveKeep", status=FilterStatus.ARCHIVED),
            self._make_filter("ArchiveSkip", status=FilterStatus.ARCHIVED),
        ]
        engine = ConsolidationEngine()

        consolidated, report = engine.consolidate(
            [], archived_filters=archived_filters, exclude_names={"ArchiveSkip"},
        )

        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "ArchiveKeep" in all_sources
        assert "ArchiveSkip" not in all_sources

    def test_mixed_four_statuses(self):
        """All four statuses in one consolidation."""
        enabled = self._make_filter("Enabled")
        disabled = self._make_filter("Disabled", status=FilterStatus.DISABLED)
        archived = self._make_filter("Archived", status=FilterStatus.ARCHIVED)
        deprecated = self._make_filter("Deprecated", status=FilterStatus.DEPRECATED)

        engine = ConsolidationEngine()
        consolidated, report = engine.consolidate(
            [enabled, disabled, deprecated],
            archived_filters=[archived],
        )

        all_sources = []
        for cf in consolidated:
            all_sources.extend(cf.source_filters)
        assert "Enabled" in all_sources
        assert "Archived" in all_sources
        assert "Deprecated" not in all_sources
        assert "Disabled" not in all_sources  # disabled skipped by default

    def test_report_has_archived_and_excluded_counts(self):
        """ConsolidationReport includes archived_count and excluded_count."""
        engine = ConsolidationEngine()
        consolidated, report = engine.consolidate(
            [self._make_filter("A"), self._make_filter("B")],
            archived_filters=[self._make_filter("C", status=FilterStatus.ARCHIVED)],
            exclude_names={"B"},
        )
        assert report.archived_count == 1
        assert report.excluded_count == 1
