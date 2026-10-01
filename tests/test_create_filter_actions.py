"""move_to_dropdown_label: which "Move to" entry create_filter picks for an action."""

import pytest

from src.models.filter_models import FilterAction, SYSTEM_FOLDER_ACTIONS, ActionType
from src.scraper.protonmail_sync import move_to_dropdown_label


@pytest.mark.parametrize("label, entry", sorted(SYSTEM_FOLDER_ACTIONS.items()))
def test_system_folders_round_trip(label, entry):
    """Each system folder the scraper records maps back to its dropdown label."""
    assert move_to_dropdown_label(entry) == label


@pytest.mark.parametrize("folder, expected", [
    ("Work", "Work"),
    ("Work/Clients", "Clients"),
    ("Work/Misc\\/Others", "Misc/Others"),
    ("A\\/B", "A/B"),
])
def test_user_folder_is_last_segment_unescaped(folder, expected):
    action = FilterAction(type=ActionType.MOVE_TO, parameters={"folder": folder})
    assert move_to_dropdown_label(action.model_dump(mode="json")) == expected


def test_trash_action_from_model():
    assert move_to_dropdown_label(FilterAction(type=ActionType.TRASH).model_dump(mode="json")) == "Trash"


@pytest.mark.parametrize("action_type", ["mark_read", "star", "label"])
def test_non_folder_actions_have_no_dropdown_entry(action_type):
    assert move_to_dropdown_label({"type": action_type, "parameters": {}}) is None
