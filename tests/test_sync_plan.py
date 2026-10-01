"""Unit tests for src.backup.sync_plan: which live filters sync may disable."""

from pathlib import Path

import pytest

from src.backup.sync_plan import (
    carried_hashes, check_disable_candidates, incomplete_in_script, incompleteness_reasons,
    manifest_describes, plan_disable, rules_in_script,
)
from src.consolidator.carry_forward import CARRIED_PREFIX, filter_facts
from src.models.filter_models import (
    ActionType, ConditionType, FilterAction, FilterCondition, LogicType, Operator,
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
    plan = plan_disable([off], {off.content_hash}, [off], SIEVE_NAME, filter_facts(off))
    for name, bucket in _buckets(plan).items():
        assert off not in bucket, name


def test_plan_disable_sieve_filter_never_disabled_even_if_carried():
    own, other = _sieve(SIEVE_NAME), _sieve("Hand-written")
    carried = {own.content_hash, other.content_hash}
    every_fact = filter_facts(own) | filter_facts(other)
    plan = plan_disable([own, other], carried, [own, other], SIEVE_NAME, every_fact)
    assert plan.to_disable == []
    assert plan.sieve == [own, other]


def test_check_disable_candidates_rejects_disabled_and_sieve():
    with pytest.raises(ValueError, match="not enabled"):
        check_disable_candidates([_wizard("a@x.com", enabled=False)])
    with pytest.raises(ValueError, match="Sieve"):
        check_disable_candidates([_sieve("Hand-written")])
    check_disable_candidates([_wizard("a@x.com")])


def test_carried_hashes_fallback_excludes_sieve(tmp_path):
    wizard, sieve = _wizard("a@x.com"), _sieve("Hand-written")
    hashes, from_manifest = carried_hashes(None, tmp_path / "x.sieve", [wizard, sieve])
    assert from_manifest is False
    assert hashes == {wizard.content_hash}


def test_carried_hashes_ignores_manifest_for_other_script(tmp_path):
    a, b = _wizard("a@x.com"), _wizard("b@x.com")
    own = tmp_path / "consolidated.sieve"
    other = tmp_path / "other.sieve"
    own.write_text("keep;")
    other.write_text("keep;")
    manifest = {"sieve_file": str(own), "filter_hashes": [a.content_hash]}

    assert carried_hashes(manifest, own, [a, b]) == ({a.content_hash}, True)
    hashes, from_manifest = carried_hashes(manifest, other, [a, b])
    assert from_manifest is False
    assert hashes == {a.content_hash, b.content_hash}


def test_carried_filter_whose_rules_are_not_in_the_script_stays_enabled():
    """Whatever the manifest says, a filter is disabled only if the script holds its rules."""
    a, b = _wizard("a@x.com"), _wizard("b@x.com")
    carried = {a.content_hash, b.content_hash}
    plan = plan_disable([a, b], carried, [a, b], SIEVE_NAME, filter_facts(a))
    assert plan.to_disable == [a]
    assert plan.not_covered == [b]
    assert b in plan.left_enabled


def test_partly_covered_filter_is_not_covered():
    two_senders = _wizard("a@x.com").model_copy(update={
        "name": "Two senders",
        "conditions": [
            FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a@x.com"),
            FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="b@x.com"),
        ],
        "logic": LogicType.OR,
    })
    only_a = filter_facts(_wizard("a@x.com"))
    assert not rules_in_script(two_senders, only_a)
    assert rules_in_script(two_senders, filter_facts(two_senders))


def test_filter_with_no_rules_is_never_covered(monkeypatch):
    import src.backup.sync_plan as sync_plan
    monkeypatch.setattr(sync_plan, "filter_facts", lambda f: set())
    assert not rules_in_script(_wizard("a@x.com"), set())


def test_manifest_describes_relative_and_absolute_spellings(tmp_path, monkeypatch):
    script = tmp_path / "out.sieve"
    script.write_text("keep;")
    monkeypatch.chdir(tmp_path)
    assert manifest_describes({"sieve_file": "out.sieve"}, script)
    assert manifest_describes({"sieve_file": str(script)}, Path("out.sieve"))
    assert manifest_describes({"sieve_file": str(tmp_path / "." / "out.sieve")}, script)
    assert not manifest_describes({"sieve_file": "other.sieve"}, script)
    assert not manifest_describes({}, script)
    assert not manifest_describes(None, script)


def test_incompleteness_reasons():
    assert incompleteness_reasons(_wizard("a@x.com")) == []
    issue = _wizard("a@x.com").model_copy(update={"scrape_issues": ["condition 0: no value found"]})
    assert incompleteness_reasons(issue) == ["condition 0: no value found"]
    legacy = _wizard("a@x.com").model_copy(update={"raw": None})
    assert incompleteness_reasons(legacy) == ["no raw evidence (backed up before format 1.1)"]
    carried = legacy.model_copy(update={"name": f"{CARRIED_PREFIX}a"})
    assert incompleteness_reasons(carried) == []


def test_incomplete_in_script_by_facts_or_manifest_hash_listed_once():
    in_script = _wizard("a@x.com").model_copy(update={"scrape_issues": ["x"]})
    by_hash = _wizard("b@x.com").model_copy(update={"raw": None})
    elsewhere = _wizard("c@x.com").model_copy(update={"scrape_issues": ["x"]})
    complete = _wizard("d@x.com")
    facts = filter_facts(in_script) | filter_facts(complete)
    found = incomplete_in_script(
        [in_script, in_script, by_hash, elsewhere, complete], facts, {by_hash.content_hash},
    )
    assert found == [in_script, by_hash]
