"""Tests for carrying live Sieve rules forward into archived filters."""

import json

import pytest
from typer.testing import CliRunner

from src.main import app
from src.backup.backup_manager import BackupManager
from src.consolidator.carry_forward import CARRIED_PREFIX, facts_to_filters, filter_facts, label_targets
from src.consolidator.consolidation_engine import ConsolidationEngine
from src.generator.sieve_generator import SECTION_BEGIN, SECTION_END, SieveGenerator
from src.generator.sieve_rules import compare_sections, script_facts
from src.models.filter_models import (
    ProtonMailFilter, FilterCondition, FilterAction, FilterStatus,
    ConditionType, Operator, ActionType, LogicType, ScrapeEvidence,
)
from tests.test_sync_safety import (  # noqa: F401  (fixtures)
    _filter, _section_for, _wide_console, cli_snapshots_dir, fake_sync, shrunk_account,
)

runner = CliRunner()


def _generated_facts(filters):
    consolidated, _ = ConsolidationEngine().consolidate(filters, include_disabled=True)
    return script_facts(SieveGenerator().generate(consolidated))


VARIED_FILTERS = [
    _filter("a@x.com"),
    _filter("b@x.com"),
    ProtonMailFilter(
        name="recipient",
        conditions=[FilterCondition(type=ConditionType.RECIPIENT, operator=Operator.CONTAINS, value="me@")],
        actions=[FilterAction(type=ActionType.MARK_READ), FilterAction(type=ActionType.STAR)],
    ),
    ProtonMailFilter(
        name="and group",
        logic=LogicType.AND,
        conditions=[
            FilterCondition(type=ConditionType.SENDER, operator=Operator.CONTAINS, value="news"),
            FilterCondition(type=ConditionType.SUBJECT, operator=Operator.MATCHES, value="Weekly*"),
        ],
        actions=[FilterAction(type=ActionType.ARCHIVE)],
    ),
    ProtonMailFilter(
        name="or group",
        logic=LogicType.OR,
        conditions=[
            FilterCondition(type=ConditionType.SUBJECT, operator=Operator.CONTAINS, value="[SPAM]"),
            FilterCondition(type=ConditionType.HEADER, operator=Operator.IS, value="yes"),
        ],
        actions=[FilterAction(type=ActionType.TRASH)],
    ),
    ProtonMailFilter(
        name="attachments",
        conditions=[FilterCondition(type=ConditionType.ATTACHMENTS, operator=Operator.HAS)],
        actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Parent/Child \"q\""})],
    ),
    ProtonMailFilter(
        name="no actions",
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="keepme")],
    ),
]


class TestFactsToFilters:

    def test_round_trip_reproduces_every_fact(self):
        facts = _generated_facts(VARIED_FILTERS)
        filters, unconvertible = facts_to_filters(facts)
        assert unconvertible == []
        assert _generated_facts(filters) == facts
        assert all(f.status == FilterStatus.ARCHIVED and not f.enabled for f in filters)
        assert all(f.name.startswith(CARRIED_PREFIX) for f in filters)

    def test_senders_with_same_action_are_packed(self):
        facts = _generated_facts([_filter(f"s{i}") for i in range(50)])
        filters, _ = facts_to_filters(facts)
        assert len(filters) == 1
        assert len(filters[0].conditions[0].values) == 50

    def test_fileinto_is_move_to_without_label_info(self):
        """The Sieve cannot tell a label from a folder, so the default is MOVE_TO."""
        (f,), _ = facts_to_filters(_generated_facts([_filter("a", folder="Work")]))
        assert [a.type for a in f.actions] == [ActionType.MOVE_TO]

    def test_known_label_stays_label(self):
        labelled = ProtonMailFilter(
            name="L",
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a")],
            actions=[FilterAction(type=ActionType.LABEL, parameters={"label": "Receipts"})],
        )
        facts = _generated_facts([labelled])
        (f,), unconvertible = facts_to_filters(facts, label_names=label_targets([labelled]))
        assert unconvertible == []
        assert f.actions == [FilterAction(type=ActionType.LABEL, parameters={"label": "Receipts"})]
        assert _generated_facts([f]) == facts

    def test_name_used_as_label_and_folder_falls_back_to_move_to(self):
        both = [
            ProtonMailFilter(name="L", actions=[FilterAction(type=ActionType.LABEL, parameters={"label": "Work"})]),
            ProtonMailFilter(name="M", actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": "Work"})]),
        ]
        assert label_targets(both) == set()

    def test_label_makes_names_unique_per_run(self):
        facts = _generated_facts([_filter("a")])
        (f1,), _ = facts_to_filters(facts, label="2026-01-01")
        (f2,), _ = facts_to_filters(facts, label="2026-02-01")
        assert f1.name != f2.name

    @pytest.mark.parametrize("sieve", [
        'if address :is "From" "a" { fileinto "X"; stop; }',
        'if not exists "X-Foo" { discard; }',
        'if header :contains "X-Other" "v" { discard; }',
        'if address :domain :is "From" "x.com" { discard; }',
        'if address :is "From" "a" { redirect "b@x.com"; }',
        'if true { keep; } else { discard; }',
    ])
    def test_unrepresentable_rules_are_reported_not_approximated(self, sieve):
        filters, unconvertible = facts_to_filters(script_facts(sieve))
        assert filters == []
        assert unconvertible


class TestConsolidateKeepLiveRules:

    def test_carried_label_rule_keeps_label_type(self, cli_snapshots_dir, fake_sync):
        """A live rule filing into a name the backup knows as a label comes back as LABEL."""
        def labelled(sender):
            return ProtonMailFilter(
                name=f"L {sender}",
                conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value=sender)],
                actions=[FilterAction(type=ActionType.LABEL, parameters={"label": "Receipts"})],
                raw=ScrapeEvidence(conditions_text="the sender", actions_text="Label as Receipts"),
            )
        survivor, deleted = labelled("kept@x.com"), labelled("gone@x.com")
        live = _section_for([survivor, deleted])
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([survivor], sieve_script=live)

        result = runner.invoke(app, ["consolidate", "--keep-live-rules"])
        assert result.exit_code == 0, result.output
        carried = [
            e.filter for e in manager.load_archive(manager.snapshot_dir_for("latest"))
            if e.filter.name.startswith(CARRIED_PREFIX)
        ]
        assert [a.type for f in carried for a in f.actions] == [ActionType.LABEL]

    def test_without_flag_warns(self, shrunk_account):
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0
        assert "--keep-live-rules" in result.output

    def test_flag_carries_rules_and_sync_proceeds(self, shrunk_account, fake_sync, cli_snapshots_dir):
        result = runner.invoke(app, ["consolidate", "--keep-live-rules"])
        assert result.exit_code == 0, result.output
        assert "Carried forward 18" in result.output

        manager = BackupManager(cli_snapshots_dir)
        snapshot_dir = manager.snapshot_dir_for("latest")
        sieve = (snapshot_dir / "consolidated.sieve").read_text()
        assert compare_sections(shrunk_account, sieve).is_safe

        archive = manager.load_archive(snapshot_dir)
        assert any(e.filter.name.startswith(CARRIED_PREFIX) for e in archive)
        args = json.loads((snapshot_dir / "consolidation_args.json").read_text())
        assert args["keep_live_rules"] is True
        assert args["carried_forward"] > 0

        dry = runner.invoke(app, ["sync", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "carried forward" in dry.output

        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert ("disable", "Filter s0@x.com") in fake_sync.calls

    def test_rerun_is_idempotent(self, shrunk_account, cli_snapshots_dir):
        runner.invoke(app, ["consolidate", "--keep-live-rules"])
        manager = BackupManager(cli_snapshots_dir)
        before = len(manager.load_archive(manager.snapshot_dir_for("latest")))
        result = runner.invoke(app, ["consolidate", "--keep-live-rules"])
        assert result.exit_code == 0
        assert len(manager.load_archive(manager.snapshot_dir_for("latest"))) == before

    def test_next_backup_cycle_needs_no_flag(self, shrunk_account, fake_sync, cli_snapshots_dir):
        """Carried rules live in archive.json, which every later backup inherits."""
        import time
        runner.invoke(app, ["consolidate", "--keep-live-rules"])
        manager = BackupManager(cli_snapshots_dir)
        time.sleep(1)  # snapshot dirs are named by the second
        manager.create_backup([_filter("s0@x.com"), _filter("s1@x.com", folder="F1")],
                              sieve_script=shrunk_account)
        result = runner.invoke(app, ["consolidate"])
        assert result.exit_code == 0
        assert "Live Rules Would Be Dropped" not in result.output
        assert runner.invoke(app, ["sync"]).exit_code == 0

    def test_deprecated_filter_is_not_resurrected(self, cli_snapshots_dir, fake_sync):
        keep, drop = _filter("keep@x.com"), _filter("drop@x.com")
        fake_sync.live_script = _section_for([keep, drop])
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([keep, drop], sieve_script=fake_sync.live_script)
        assert runner.invoke(app, ["snapshot", "set-status", drop.name, "deprecated"]).exit_code == 0

        result = runner.invoke(app, ["consolidate", "--keep-live-rules"])
        assert result.exit_code == 0, result.output
        sieve = (manager.snapshot_dir_for("latest") / "consolidated.sieve").read_text()
        assert "drop@x.com" not in sieve

        # Removing it from the live section still needs the explicit override
        assert runner.invoke(app, ["sync"]).exit_code == 1
        assert runner.invoke(app, ["sync", "--allow-rule-removal"]).exit_code == 0

    def test_excluded_filter_is_not_resurrected(self, cli_snapshots_dir, fake_sync):
        keep, drop = _filter("keep@x.com"), _filter("drop@x.com")
        fake_sync.live_script = _section_for([keep, drop])
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([keep, drop], sieve_script=fake_sync.live_script)

        result = runner.invoke(app, ["consolidate", "--keep-live-rules", "--exclude", drop.name])
        assert result.exit_code == 0, result.output
        sieve = (manager.snapshot_dir_for("latest") / "consolidated.sieve").read_text()
        assert "drop@x.com" not in sieve

    @pytest.mark.parametrize("operator, value, generated, legacy", [
        # Old Move to Trash: discard
        (Operator.IS, "drop@x.com", 'fileinto "trash";', "discard;"),
        # Old begins-with: no wildcard, and discard for Trash
        (Operator.STARTS_WITH, "drop", '"drop*"', '"drop"'),
    ])
    def test_deprecated_filter_not_resurrected_from_legacy_live_rule(
        self, cli_snapshots_dir, fake_sync, operator, value, generated, legacy,
    ):
        """The live rule an older version wrote for a deprecated filter stays dropped."""
        keep = _filter("keep@x.com")
        drop = ProtonMailFilter(
            name="Drop", raw=ScrapeEvidence(conditions_text="the sender", actions_text="Move to Trash"),
            conditions=[FilterCondition(type=ConditionType.SENDER, operator=operator, value=value)],
            actions=[FilterAction(type=ActionType.TRASH)],
        )
        current = _section_for([keep, drop])
        assert generated in current
        live = current.replace(generated, legacy).replace('fileinto "trash";', "discard;")
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([keep, drop], sieve_script=live)
        assert runner.invoke(app, ["snapshot", "set-status", drop.name, "deprecated"]).exit_code == 0

        result = runner.invoke(app, ["consolidate", "--keep-live-rules"])
        assert result.exit_code == 0, result.output
        sieve = (manager.snapshot_dir_for("latest") / "consolidated.sieve").read_text()
        assert "drop" not in sieve
        assert "Not carried forward (deprecated or --exclude'd on purpose): 1 pairs" in result.output

    def test_unconvertible_rule_reported(self, cli_snapshots_dir, fake_sync):
        from src.generator.sieve_generator import SECTION_BEGIN, SECTION_END
        live = (f'{SECTION_BEGIN}\nif address :is "From" "a" {{ fileinto "X"; stop; }}\n'
                f'{SECTION_END}\n')
        manager = BackupManager(cli_snapshots_dir)
        manager.create_backup([_filter("other@x.com")], sieve_script=live)
        result = runner.invoke(app, ["consolidate", "--keep-live-rules"])
        assert result.exit_code == 0
        assert "could not be converted" in result.output


class TestWildcardOperatorsCarryForward:
    """Live :matches patterns of the generated begins/ends-with shape come back as those operators."""

    @pytest.mark.parametrize("pattern, operator, value", [
        ("news*", Operator.STARTS_WITH, "news"),
        ("*@x.com", Operator.ENDS_WITH, "@x.com"),
        ("a\\\\*b*", Operator.STARTS_WITH, "a*b"),
        ("a*b*", Operator.MATCHES, "a*b*"),
        ("*news*", Operator.MATCHES, "*news*"),
        ("news", Operator.MATCHES, "news"),
        ("*", Operator.MATCHES, "*"),
    ])
    def test_pattern_maps_to_operator(self, pattern, operator, value):
        sieve = f'if address :matches "From" "{pattern}" {{ fileinto "trash"; }}'
        (f,), unconvertible = facts_to_filters(script_facts(sieve))
        assert unconvertible == []
        (cond,) = f.conditions
        assert (cond.operator, cond.value) == (operator, value)
        assert _generated_facts([f]) == script_facts(sieve)

    def test_round_trip_of_generated_begins_and_ends_with(self):
        filters = [
            ProtonMailFilter(
                name=f"{op.value}",
                conditions=[FilterCondition(type=ConditionType.SUBJECT, operator=op, value=v)],
                actions=[FilterAction(type=ActionType.TRASH)],
            )
            for op, v in [(Operator.STARTS_WITH, "Re: [x]"), (Operator.ENDS_WITH, "?!*")]
        ]
        facts = _generated_facts(filters)
        carried, unconvertible = facts_to_filters(facts)
        assert unconvertible == []
        assert {c.operator for f in carried for c in f.conditions} == {Operator.STARTS_WITH, Operator.ENDS_WITH}
        assert _generated_facts(carried) == facts


class TestLegacyTrashCarryForward:
    """A live `discard;` (the old Move to Trash) is carried forward as a Trash move."""

    def test_discard_carried_as_trash(self):
        live = script_facts('if address :is "From" "a@x.com" { discard; }')
        (f,), unconvertible = facts_to_filters(live)
        assert unconvertible == []
        assert [a.type for a in f.actions] == [ActionType.TRASH]
        script = SieveGenerator().generate(ConsolidationEngine().consolidate([f], include_disabled=True)[0])
        assert 'fileinto "trash";' in script
        assert "discard" not in script
        # The live section and the carried rule pair up as a correction
        live_script = f'{SECTION_BEGIN}\nif address :is "From" "a@x.com" {{ discard; }}\n{SECTION_END}\n'
        result = compare_sections(live_script, script)
        assert result.is_safe
        assert len(result.folder_fixes) == 1

    def test_fileinto_trash_is_trash_action(self):
        (f,), _ = facts_to_filters(script_facts('if address :is "From" "a" { fileinto "trash"; }'))
        assert [a.type for a in f.actions] == [ActionType.TRASH]

    def test_unconvertible_discard_fact_returned_as_given(self):
        facts = script_facts('if not exists "X-Foo" { discard; }')
        filters, unconvertible = facts_to_filters(facts)
        assert filters == []
        assert set(unconvertible) == facts


class TestFilterFactsOfUngeneratableFilter:
    """filter_facts must not raise for a filter the generator refuses."""

    def test_conditionless_filter_is_never_covered(self):
        empty = ProtonMailFilter(name="lost its condition", actions=[FilterAction(type=ActionType.TRASH)])
        facts = filter_facts(empty)
        (fact,) = facts
        assert "no conditions" in fact.describe()
        live = script_facts(SieveGenerator.merge_with_existing(
            SieveGenerator().generate(ConsolidationEngine().consolidate(
                [_filter("a@x.com")], include_disabled=True)[0]), ""))
        assert not facts <= live


class TestAttachmentCarryForward:
    """Only the generator's `exists "X-Attached"` maps back to an attachment condition."""

    def test_exists_x_attached_round_trips(self):
        facts = script_facts('if exists "X-Attached" { fileinto "Receipts"; }')
        (f,), unconvertible = facts_to_filters(facts)
        assert unconvertible == []
        (cond,) = f.conditions
        assert (cond.type, cond.operator) == (ConditionType.ATTACHMENTS, Operator.HAS)
        assert _generated_facts([f]) == facts

    @pytest.mark.parametrize("sieve", [
        'if true { fileinto "trash"; }',
        'fileinto "trash";',
        'if allof (true, address :is "From" "a") { fileinto "trash"; }',
    ])
    def test_true_is_not_an_attachment_condition(self, sieve):
        filters, unconvertible = facts_to_filters(script_facts(sieve))
        assert filters == []
        assert unconvertible


class TestMultiValueCarryForward:
    """Key lists round-trip as values lists; a literal with ", " stays literal."""

    def test_literal_with_comma_round_trips(self):
        facts = script_facts('if header :contains "Subject" "Invoice, Receipt" { fileinto "trash"; }')
        (f,), unconvertible = facts_to_filters(facts)
        assert unconvertible == []
        assert f.conditions[0].keys == ["invoice, receipt"]
        assert _generated_facts([f]) == facts

    def test_key_with_pipe_round_trips(self):
        facts = script_facts('if header :contains "Subject" ["a|b", "c"] { fileinto "trash"; }')
        (f,), unconvertible = facts_to_filters(facts)
        assert unconvertible == []
        assert sorted(f.conditions[0].values) == ["a|b", "c"]
        assert _generated_facts([f]) == facts


@pytest.mark.parametrize("target", ["Archive", "archive"])
def test_archive_any_case_carries_as_archive(target):
    facts = script_facts(f'if address :is "From" "a" {{ fileinto "{target}"; }}')
    (f,), unconvertible = facts_to_filters(facts)
    assert unconvertible == []
    assert [a.type for a in f.actions] == [ActionType.ARCHIVE]
    assert _generated_facts([f]) == facts


def test_live_archive_counts_as_covering_archive_filter():
    """cleanup coverage: an old live "Archive" rule covers a current archive filter."""
    f = ProtonMailFilter(
        name="arch",
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a")],
        actions=[FilterAction(type=ActionType.ARCHIVE)],
    )
    live = script_facts('if address :is "From" "a" { fileinto "Archive"; }')
    assert filter_facts(f) <= live


def test_escaped_slash_in_folder_round_trips():
    """'Misc/Others' inside 'Work' survives carry-forward without double escaping."""
    from src.models.filter_models import join_folder_path
    folder = join_folder_path(["Work", "Misc/Others"])
    f = ProtonMailFilter(
        name="nested",
        conditions=[FilterCondition(type=ConditionType.SENDER, operator=Operator.IS, value="a")],
        actions=[FilterAction(type=ActionType.MOVE_TO, parameters={"folder": folder})],
    )
    facts = _generated_facts([f])
    (carried,), unconvertible = facts_to_filters(facts)
    assert unconvertible == []
    assert carried.actions[0].parameters == {"folder": "Work/Misc\\/Others"}
    assert _generated_facts([carried]) == facts


def test_candidate_with_extra_facts_is_unconvertible(monkeypatch):
    """A candidate whose regeneration yields MORE than its facts is refused.

    Verification must be equality: a candidate that also produces some
    other fact would add a rule nobody had, so a superset is not a match.
    """
    import src.consolidator.carry_forward as carry_forward

    facts = script_facts('if address :is "From" "a" { fileinto "trash"; }')
    extra = next(iter(script_facts('if address :is "From" "zzz" { fileinto "trash"; }')))
    real_filter_facts = carry_forward.filter_facts
    monkeypatch.setattr(carry_forward, "filter_facts", lambda f: real_filter_facts(f) | {extra})

    filters, unconvertible = facts_to_filters(facts)
    assert filters == []
    assert set(unconvertible) == facts


def test_carried_key_containing_pipe_survives_archive_round_trip(tmp_path):
    """V4: a live rule whose single key contains "|" is carried forward as one
    literal and stays one after archive.json is written and read back, so the
    regenerated rule is not widened into an OR of "invoice" and "receipt"."""
    from src.backup.backup_manager import BackupManager
    from src.consolidator.consolidation_engine import ConsolidationEngine
    from src.generator.sieve_generator import SieveGenerator
    from src.generator.sieve_rules import script_facts
    from src.models.backup_models import ArchiveEntry, BACKUP_FORMAT_VERSION
    live = ('# === BEGIN ProtonFusion ===\n'
            'if header :contains "Subject" "invoice|receipt" {\n    fileinto "Bills";\n}\n'
            '# === END ProtonFusion ===\n')
    carried, unconvertible = facts_to_filters(script_facts(live), label="snap")
    assert unconvertible == []
    manager = BackupManager(tmp_path)
    manager.write_archive(tmp_path, [ArchiveEntry(filter=f, source_format=BACKUP_FORMAT_VERSION) for f in carried])
    back = [e.filter for e in manager.load_archive(tmp_path)]
    assert [c.keys for c in back[0].conditions] == [["invoice|receipt"]]
    consolidated, _ = ConsolidationEngine().consolidate([], archived_filters=back)
    assert script_facts(SieveGenerator().generate(consolidated)) == script_facts(live)
