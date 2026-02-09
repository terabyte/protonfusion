"""Strategy: Optimize filter ordering for efficiency."""

import logging
from typing import List

from src.models.filter_models import ConsolidatedFilter, ActionType

logger = logging.getLogger(__name__)

# Priority ordering: lower number = higher priority (evaluated first)
ACTION_PRIORITY = {
    ActionType.DELETE: 0,      # Spam/delete first (most common, stops processing)
    ActionType.ARCHIVE: 1,     # Archive next
    ActionType.MOVE_TO: 2,     # Folder routing
    ActionType.LABEL: 3,       # Labeling
    ActionType.MARK_READ: 4,   # Mark as read
    ActionType.STAR: 5,        # Star
}


def optimize_ordering(filters: List[ConsolidatedFilter]) -> List[ConsolidatedFilter]:
    """Sort consolidated filters by priority.

    Delete/spam rules come first (most impactful, can stop processing).
    Then folder moves, labels, etc.
    Also sorts by filter count (more consolidated = higher priority within same action type).
    """
    def sort_key(f: ConsolidatedFilter):
        # Get the highest priority action in this filter
        min_priority = 99
        for action in f.actions:
            p = ACTION_PRIORITY.get(action.type, 10)
            min_priority = min(min_priority, p)
        # Secondary sort by filter count (more consolidated = earlier)
        return (min_priority, -f.filter_count)

    sorted_filters = sorted(filters, key=sort_key)
    logger.info("Optimized ordering for %d filters", len(sorted_filters))
    return sorted_filters
