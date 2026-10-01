"""Unit tests for src.backup.sync_plan: which live filters sync may disable."""

import pytest

from src.backup.sync_plan import check_disable_candidates, plan_disable
from src.models.filter_models import (
    ActionType, ConditionType, FilterAction, FilterCondition, Operator,
    ProtonMailFilter, ScrapeEvidence,
)

SIEVE_NAME = "ProtonFusion Consolidated"


def _wizard(sender: str, enabled: bool = True) -> ProtonMailFilter:
    """A fully scraped wizard filter moving one sender's mail to Spam."""
    return ProtonMailFilter(
        name=f"Filter {sender}",
        enabled=enabled,
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value=sender)],
        actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Spam"})],
        raw=ScrapeEvidence(conditions_text="the sender", actions_text="Move to Spam"),
    )


def _sieve(name: str) -> ProtonMailFilter:
    return ProtonMailFilter(name=name, is_sieve=True, raw=ScrapeEvidence(sieve_text="keep;"))


def _buckets(plan):
    return {
        "to_disable": plan.to_disable, "sieve": plan.sieve, "not_in_script": plan.not_in_script,
        "after_backup": plan.after_backup, "unreadable": plan.unreadable,
    }


def test_user_disabled_filter_is_in_no_bucket():
    off = _wizard("off@x.com", enabled=False)
    plan = plan_disable([off], {off.content_hash}, [off], SIEVE_NAME)
    for name, bucket in _buckets(plan).items():
        assert off not in bucket, name


def test_plan_disable_sieve_filter_never_disabled_even_if_carried():
    own, other = _sieve(SIEVE_NAME), _sieve("Hand-written")
    carried = {own.content_hash, other.content_hash}
    plan = plan_disable([own, other], carried, [own, other], SIEVE_NAME)
    assert plan.to_disable == []
    assert plan.sieve == [own, other]


def test_check_disable_candidates_rejects_disabled_and_sieve():
    with pytest.raises(ValueError, match="not enabled"):
        check_disable_candidates([_wizard("a@x.com", enabled=False)])
    with pytest.raises(ValueError, match="Sieve"):
        check_disable_candidates([_sieve("Hand-written")])
    check_disable_candidates([_wizard("a@x.com")])
