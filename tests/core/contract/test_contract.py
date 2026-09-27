"""
The declarative contract layer, tested in both directions.

Two kinds of coverage, mirroring the other harness self-test modules:

* **Design validation** — every way a declaration can be incomplete or
  contradictory is refused at construction with a message naming the mistake,
  because a contract that constructs is a contract whose omissions became
  invisible.
* **A generated suite that actually runs** — child pytest sessions exercise
  conforming, deliberately broken and repaired reference cases. The parent
  tests assert their outcomes, so testing XFAIL/strict XPASS never leaves
  artificial known failures in the main suite.

The database marks a generated case carries come from the configured host; the
``marking_host`` fixture supplies a stand-in mark so the transactional choices
are observable without a database.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sample_production import tasks

from due_work_harness.contract import (
    Adoption,
    Claim,
    Decline,
    DueWorkContract,
    DueWorkContractDesignError,
    DueWorkSource,
    ExtraProof,
    KnownGap,
    NotApplicable,
    Profile,
    ScheduledSelection,
    contract_cases,
    contract_report,
    due_work_contract_suite,
    safety_contract_cases,
    scheduled_selection_cases,
)
from due_work_harness.crash_histories import CallableDelivery, HandoffHistory
from due_work_harness.gap_probes import (
    DisprovenCapability,
    MissingReclaim,
    MissingScheduledConsumer,
)
from due_work_harness.host import Host
from due_work_harness.profiles.automatic_recovery import assert_in_flight_work_is_not_redispatched
from due_work_harness.profiles.fact_derived_obligations import (
    StateDerived,
    assert_unrecorded_obligation_is_discovered,
)
from due_work_harness.references import in_memory_handoffs
from due_work_harness.references.in_memory import (
    EdgeTriggeredDeriver,
    MaterialisingDeriver,
    assert_self_test_probe_fires,
    assert_the_reference_capability_exists,
    materialising_derivation_binding,
)
from tests.core.contract.declarations import (
    REFERENCE_CONTRACT,
    SELF_TEST_NO_PRODUCTION,
    WHY,
    annotated_empty_selection,
    annotated_extra,
    annotated_never_built,
    derivation_binding,
    dispositions,
    safety_contract,
    snapshot_binding,
)


def _reference_due_work_source() -> None:
    """A callable used only to pin typed source metadata behavior."""


def test_meaningful_profile_names_are_aliases_of_the_letters() -> None:
    aliases = {
        Profile.AUTOMATIC_RECOVERY: Profile.A,
        Profile.BOUNDED_OWNERSHIP: Profile.B,
        Profile.CRASH_AMBIGUITY: Profile.C,
        Profile.DURABLE_RETENTION: Profile.D,
        Profile.EVENTUAL_CONVERGENCE: Profile.E,
        Profile.FACT_DERIVED_OBLIGATIONS: Profile.F,
    }

    assert all(meaningful is letter for meaningful, letter in aliases.items())
    assert [profile.name for profile in Profile] == ["A", "B", "C", "D", "E", "F"]


# Design validation.


def _contract(**overrides: Any) -> DueWorkContract:
    """A valid baseline contract; keyword overrides introduce the defect under test."""
    arguments: dict[str, Any] = {
        "name": "self-test contract",
        "profiles": dispositions(F=Claim()),
        "safety": safety_contract(),
        "derivation": derivation_binding,
    }
    arguments.update(overrides)
    return DueWorkContract(**arguments)


def test_a_complete_declaration_constructs() -> None:
    _contract()


def test_a_due_work_adopter_cannot_omit_the_safety_profiles() -> None:
    with pytest.raises(DueWorkContractDesignError, match="no safety contract declares"):
        _contract(safety=None)


def test_the_safety_contract_must_describe_the_same_domain() -> None:
    with pytest.raises(DueWorkContractDesignError, match="the declarations must describe the same domain"):
        _contract(safety=safety_contract("another domain"))


def test_an_omitted_profile_is_a_design_error() -> None:
    incomplete = {profile: NotApplicable(WHY) for profile in Profile if profile is not Profile.D}
    with pytest.raises(DueWorkContractDesignError, match="no disposition for profile\\(s\\) D"):
        _contract(profiles=incomplete, derivation=None)


def test_a_claim_without_a_binding_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="profile F .* claimed but has no binding"):
        _contract(derivation=None)


def test_a_binding_without_a_claim_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="profile F is NotApplicable but `derivation=` is bound"):
        _contract(profiles=dispositions())


def test_a_gap_naming_no_proof_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="gap 'assert_renamed_away' names no proof"):
        _contract(profiles=dispositions(F=Claim(gaps={"assert_renamed_away": WHY})))


def test_a_gap_on_an_unbound_half_of_profile_e_is_a_design_error() -> None:
    """A gap must name a proof that will actually run, not one of the other half's."""
    with pytest.raises(DueWorkContractDesignError, match="names no proof that will run"):
        _contract(
            profiles=dispositions(
                F=Claim(),
                E=Claim(gaps={"assert_settled_state_is_not_downgraded": WHY}),
            ),
            snapshot=snapshot_binding,
        )


def test_an_empty_decline_reason_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="profile B is Decline with no reason"):
        _contract(profiles=dispositions(F=Claim(), B=Decline("  ")))


def test_a_domain_profile_binding_requires_evidence_annotations() -> None:
    def unannotated_derivation() -> StateDerived:
        return derivation_binding()

    with pytest.raises(
        DueWorkContractDesignError,
        match="missing adopter evidence annotations.*ARRANGE.*REAL PRODUCTION.*EXTERNAL SEAM.*OBSERVE",
    ):
        _contract(derivation=unannotated_derivation)


def test_a_duplicate_extra_name_is_a_design_error() -> None:
    extras = (
        ExtraProof(name="twice", run=annotated_extra, no_production_callable_because=SELF_TEST_NO_PRODUCTION),
        ExtraProof(name="twice", run=annotated_extra, no_production_callable_because=SELF_TEST_NO_PRODUCTION),
    )
    with pytest.raises(DueWorkContractDesignError, match="duplicate extra proof name 'twice'"):
        _contract(extras=extras)


def test_every_design_error_is_reported_at_once() -> None:
    """One construction reports every defect, not just the first."""
    incomplete = {Profile.A: Decline(" ")}
    with pytest.raises(DueWorkContractDesignError) as caught:
        _contract(profiles=incomplete, derivation=derivation_binding)
    message = str(caught.value)
    assert "no disposition for profile(s) B, C, D, E, F" in message
    assert "profile A is Decline with no reason" in message


def test_scheduled_selection_refuses_a_gap_naming_no_selection_proof() -> None:
    with pytest.raises(DueWorkContractDesignError, match="not in the scheduled-selection contract"):
        ScheduledSelection(
            name="self-test selection",
            due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
            assert_scheduled=lambda: None,
            adoption=Adoption.LEGACY,
            gaps={"assert_tick_is_bounded": WHY},
        )


# Case generation.


def _params_by_id(params: list[Any]) -> dict[str, Any]:
    return {param.id: param for param in params}


def _mark_names(param: Any) -> list[str]:
    return [mark.name for mark in param.marks]


def _database_mark(param: Any) -> Any:
    return next(mark for mark in param.marks if mark.name == "database")


def test_cases_cover_every_profile_and_extra() -> None:
    contract = _contract(
        adoption=Adoption.LEGACY,
        profiles=dispositions(F=Claim(), A=KnownGap(WHY), B=Decline(WHY)),
        extras=(ExtraProof(name="probe", run=annotated_extra, no_production_callable_because=SELF_TEST_NO_PRODUCTION),),
    )
    ids = set(_params_by_id(contract_cases(contract)))
    assert "A-known_gap" in ids
    assert "B-declined" in ids
    assert "C-not_applicable" in ids
    assert "F-assert_unrecorded_obligation_is_discovered" in ids
    assert "extra-probe" in ids


def test_a_claim_gap_becomes_a_strict_xfail_with_its_reason() -> None:
    contract = _contract(
        adoption=Adoption.LEGACY,
        profiles=dispositions(F=Claim(gaps={"assert_stopped_work_is_not_revived": "stop predicate missing"})),
    )
    params = _params_by_id(contract_cases(contract))
    marks = {mark.name: mark for mark in params["F-assert_stopped_work_is_not_revived"].marks}
    assert marks["xfail"].kwargs == {"strict": True, "reason": "stop predicate missing"}
    clean = params["F-assert_derivation_is_idempotent"]
    assert "xfail" not in _mark_names(clean)


def test_claiming_a_and_f_together_generates_the_coherence_case() -> None:
    """
    Two green profiles can still describe different work — F deriving rows A's
    sweep never selects — so claiming both earns the cross-profile case, and a
    reviewed, deliberate split is declarable as a (waivable, behavioral) legacy
    gap on it.
    """
    contract = _contract(
        adoption=Adoption.LEGACY,
        profiles=dispositions(
            A=Claim(),
            F=Claim(
                gaps={"assert_automatic_recovery_consumes_derived_obligations": "the sweep is deliberately narrower"}
            ),
        ),
        sweep=annotated_never_built,
    )
    params = _params_by_id(contract_cases(contract))
    case = params["AF-assert_automatic_recovery_consumes_derived_obligations"]
    marks = {mark.name: mark for mark in case.marks}
    assert marks["xfail"].kwargs == {"strict": True, "reason": "the sweep is deliberately narrower"}


def test_the_coherence_case_needs_both_profiles_claimed() -> None:
    only_f = _params_by_id(contract_cases(_contract()))
    assert "AF-assert_automatic_recovery_consumes_derived_obligations" not in only_f


def test_claiming_a_and_f_generates_their_coherence_proof() -> None:
    contract = _contract(profiles=dispositions(A=Claim(), F=Claim()), sweep=annotated_never_built)
    ids = set(_params_by_id(contract_cases(contract)))
    assert "AF-assert_automatic_recovery_consumes_derived_obligations" in ids


def test_the_default_host_gives_generated_cases_no_database_marks() -> None:
    """The core asks the host for marks; with none configured, a case carries only its own."""
    contract = _contract(
        profiles=dispositions(F=Claim(), A=Claim(), B=Claim()),
        sweep=annotated_never_built,
        ownership=annotated_never_built,
    )
    for param in contract_cases(contract):
        assert not param.marks, f"{param.id} carries {param.marks} from a host that supplies no marks"


def test_cross_connection_proofs_run_transactionally(marking_host: Host) -> None:
    """Claims A and B ask the host for real commits for their concurrency proofs, and only for those."""
    contract = _contract(
        profiles=dispositions(F=Claim(), A=Claim(), B=Claim()),
        sweep=annotated_never_built,
        ownership=annotated_never_built,
    )
    params = _params_by_id(contract_cases(contract))
    for name in ("A-assert_in_flight_work_is_not_duplicated", "B-assert_claim_is_exclusive_across_connections"):
        assert _database_mark(params[name]).kwargs == {"transaction": True}, name
    assert _database_mark(params["B-assert_claim_is_exclusive"]).kwargs == {"transaction": False}


def test_autocommit_contract_marks_runtime_and_composition_proofs_transactionally(marking_host: Host) -> None:
    contract = _contract(
        transactional=True,
        profiles=dispositions(F=Claim(), A=Claim(), B=Claim()),
        sweep=annotated_never_built,
        ownership=annotated_never_built,
    )
    params = _params_by_id(contract_cases(contract))
    for name in (
        "A-assert_recovers_stranded_work",
        "B-assert_claim_is_exclusive",
        "AF-assert_automatic_recovery_consumes_derived_obligations",
    ):
        assert _database_mark(params[name]).kwargs == {"transaction": True}, name


def test_contract_fixtures_are_requested_by_every_generated_case() -> None:
    """
    A proof that needs host setup — a distinct replica alias for the replica
    routing proof, for example — gets it from the contract's ``fixtures``,
    which every generated behavioral case requests before it runs.
    """
    contract = _contract(
        profiles=dispositions(F=Claim(), A=Claim()),
        sweep=annotated_never_built,
        fixtures=("replica_alias",),
    )
    params = _params_by_id(contract_cases(contract))
    assert params["A-assert_selection_does_not_read_the_replica"].values[0].fixtures == ("replica_alias",)
    assert params["AF-assert_automatic_recovery_consumes_derived_obligations"].values[0].fixtures == ("replica_alias",)

    selection = ScheduledSelection(
        name="self-test selection",
        due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
        unscheduled_because="self-test: driven by an external scheduler",
        fixtures=("replica_alias",),
    )
    selection_params = _params_by_id(scheduled_selection_cases(selection))
    assert selection_params["assert_selection_does_not_read_the_replica"].values[0].fixtures == ("replica_alias",)


def test_a_known_gap_without_a_detect_probe_documents_itself() -> None:
    contract = _contract(adoption=Adoption.LEGACY, profiles=dispositions(F=Claim(), A=KnownGap("no sweep exists")))
    params = _params_by_id(contract_cases(contract))
    gap = params["A-known_gap"]
    marks = {mark.name: mark for mark in gap.marks}
    assert marks["xfail"].kwargs == {"strict": True, "reason": "no sweep exists"}
    with pytest.raises(pytest.fail.Exception, match="no sweep exists"):
        gap.values[0].run()


def test_a_known_gap_detect_probe_is_the_test_body() -> None:
    probed: list[bool] = []
    contract = _contract(
        adoption=Adoption.LEGACY,
        profiles=dispositions(
            F=Claim(),
            A=KnownGap("no sweep exists", detect=lambda: assert_self_test_probe_fires(probed)),
        ),
    )
    params = _params_by_id(contract_cases(contract))
    params["A-known_gap"].values[0].run()
    assert probed == [True]


def test_a_test_authored_decline_proof_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="negative proof.*production absence"):
        _contract(profiles=dispositions(F=Claim(), B=Decline(WHY, prove=lambda: assert_self_test_probe_fires([]))))


def test_bindings_are_built_fresh_per_case_run() -> None:
    built: list[int] = []

    def counting_binding() -> StateDerived:
        # ARRANGE — the reference factory creates fresh in-memory state.
        # REAL PRODUCTION — this self-test delegates to the reference transition.
        # EXTERNAL SEAM — none; this is a harness lifecycle self-test.
        # OBSERVE — the local list records factory construction count.
        built.append(len(built))
        return derivation_binding()

    contract = _contract(derivation=counting_binding)
    params = _params_by_id(contract_cases(contract))
    params["F-assert_unrecorded_obligation_is_discovered"].values[0].run()
    params["F-assert_derivation_is_idempotent"].values[0].run()
    assert built == [0, 1], "each generated case must construct its own binding"


def test_the_report_names_every_disposition() -> None:
    contract = _contract(
        adoption=Adoption.LEGACY,
        profiles=dispositions(
            F=Claim(gaps={"assert_stopped_work_is_not_revived": "stop predicate missing"}),
            A=KnownGap("no sweep exists"),
            B=Decline("no ownership surface"),
        ),
    )
    report = contract_report(contract)
    assert "Profile A (due-work recovery): known gap — no sweep exists" in report
    assert "Profile B (fenced ownership): declined — no ownership surface" in report
    assert "Profile D (retention): not applicable" in report
    assert "1 known gap(s): assert_stopped_work_is_not_revived" in report
    assert "Safety REPLAY_SAFE_EXECUTION (replay-safe execution): not applicable" in report
    assert "Safety BOUNDED_RETRY (bounded retry): not applicable" in report


def test_scheduled_selection_cases_cover_the_selection_proofs() -> None:
    selection = ScheduledSelection(
        name="self-test selection",
        due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
        assert_scheduled=lambda: None,
        adoption=Adoption.LEGACY,
        gaps={"assert_selection_is_index_served": "no index yet"},
    )
    params = _params_by_id(scheduled_selection_cases(selection))
    assert set(params) == {
        "assert_the_adapter_does_not_author_the_selection",
        "assert_selection_is_index_served",
        "assert_selection_does_not_read_the_replica",
        "assert_the_selection_is_actually_scheduled",
    }
    marks = {mark.name: mark for mark in params["assert_selection_is_index_served"].marks}
    assert marks["xfail"].kwargs == {"strict": True, "reason": "no index yet"}


def test_populate_seeds_exactly_the_index_proof() -> None:
    """
    ``populate`` runs before the plan-shape proof and before nothing else.

    A query plan over an empty table is a coin flip — a primary-key walk can
    "win" purely because it satisfies an ordering — so the index verdict needs
    representative rows, while the authorship and routing proofs must stay
    row-free.
    """
    seeded: list[bool] = []
    selection = ScheduledSelection(
        name="self-test selection",
        due_work=annotated_empty_selection,  # type: ignore[arg-type] - fails after populate
        unscheduled_because="self-test: exercising the declared-unscheduled path",
        populate=lambda: seeded.append(True),
    )
    params = _params_by_id(scheduled_selection_cases(selection))

    params["assert_the_adapter_does_not_author_the_selection"].values[0].run()
    assert seeded == [], "populate must not run for proofs that need no rows"

    # The default host has no selection inspector, so the plan proof fails —
    # after populate ran, which is what this test pins.
    with pytest.raises(AssertionError, match="needs a SelectionInspector"):
        params["assert_selection_is_index_served"].values[0].run()
    assert seeded == [True], "populate must run before the plan-shape proof"


# The unwaivable guards and the bespoke-assertion check.


def test_a_gap_on_the_authorship_proof_is_refused() -> None:
    """
    The defense cannot be xfailed away: meta-proof gaps are design errors.

    A gap entry naming the authorship tripwire would strict-xfail the one proof
    every other proof of the selection depends on — the suite would then be
    measuring a test-authored copy with the alarm formally acknowledged.
    """
    with pytest.raises(DueWorkContractDesignError, match="cannot be waived"):
        ScheduledSelection(
            name="self-test selection",
            due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
            assert_scheduled=lambda: None,
            adoption=Adoption.LEGACY,
            gaps={"assert_the_adapter_does_not_author_the_selection": WHY},
        )


def test_a_contract_gap_on_a_binding_integrity_proof_is_refused() -> None:
    """Same rule at the full-contract layer, for every profile's guard."""
    for unwaivable in (
        "assert_settlement_is_production_bound",
        "assert_outstanding_selection_is_production_bound",
        "assert_derivation_bindings_are_production_bound",
    ):
        with pytest.raises(DueWorkContractDesignError, match="cannot be waived"):
            _contract(adoption=Adoption.LEGACY, profiles=dispositions(F=Claim(gaps={unwaivable: WHY})))


def test_a_detect_that_inverts_an_assertion_locally_is_refused() -> None:
    """
    ``pytest.raises`` around a shared proof in a test-authored probe is the
    sham shape: it can "prove" anything by feeding the proof a synthetic
    binding and celebrating the failure. Inversion belongs in the root-owned
    DisprovenCapability probe. (A test-authored Decline.prove is refused
    outright, before this check even applies — see the negative-proof test.)
    """

    def inverted_detect() -> None:
        with pytest.raises(AssertionError):
            assert_the_reference_capability_exists()

    with pytest.raises(DueWorkContractDesignError, match="inverts or swallows an assertion"):
        _contract(adoption=Adoption.LEGACY, profiles=dispositions(F=Claim(), A=KnownGap(WHY, detect=inverted_detect)))


def test_a_locally_minted_assert_name_does_not_count_as_delegation() -> None:
    """
    The delegation check resolves referenced callables; a test-module helper
    merely NAMED ``assert_*`` is a hand-rolled check like any other.
    """

    def assert_minted_locally() -> None:
        """Not a harness proof; the name is the whole disguise."""

    with pytest.raises(DueWorkContractDesignError, match="delegates to no harness-defined assert_"):
        _contract(
            adoption=Adoption.LEGACY,
            profiles=dispositions(F=Claim(), A=KnownGap(WHY, detect=lambda: assert_minted_locally())),
        )


def test_a_hand_rolled_detect_probe_is_a_design_error() -> None:
    """A detect that asserts locally is the bespoke test the layer exists to remove."""

    def hand_rolled() -> None:
        raise RuntimeError("locally invented check")

    with pytest.raises(DueWorkContractDesignError, match="delegates to no harness-defined assert_"):
        _contract(adoption=Adoption.LEGACY, profiles=dispositions(F=Claim(), A=KnownGap(WHY, detect=hand_rolled)))


def test_a_root_owned_gap_probe_is_accepted_as_detect() -> None:
    """gap_probes instances are root-owned: accepted without local delegation."""
    _contract(
        adoption=Adoption.LEGACY,
        profiles=dispositions(
            F=Claim(),
            A=KnownGap(
                WHY,
                detect=MissingScheduledConsumer(
                    make_stranded=lambda: 1,
                    outstanding=lambda: [1],
                    scheduled_tasks=lambda: [],
                    task_name_fragment="never-scheduled",
                ),
            ),
        ),
    )


def test_a_bespoke_extra_is_a_design_error() -> None:
    with pytest.raises(
        DueWorkContractDesignError, match="extra proof 'bespoke'.*delegates to no harness-defined assert_"
    ):
        _contract(extras=(ExtraProof(name="bespoke", run=lambda: None),))


# The encoded gap policy.


def test_a_new_feature_contract_refuses_gap_declarations() -> None:
    """The default policy forbids gaps; waiving it must be an explicit LEGACY."""
    with pytest.raises(DueWorkContractDesignError, match="forbids gap declarations") as caught:
        _contract(
            profiles=dispositions(F=Claim(gaps={"assert_stopped_work_is_not_revived": WHY}), A=KnownGap(WHY)),
            extras=(
                ExtraProof(
                    name="broken", run=annotated_extra, gap=WHY, no_production_callable_because=SELF_TEST_NO_PRODUCTION
                ),
            ),
        )
    message = str(caught.value)
    assert "profile F claims with 1 gap(s)" in message
    assert "profile A is a KnownGap" in message
    assert "extra proof 'broken' declares a gap" in message


def test_legacy_adoption_permits_gap_declarations() -> None:
    _contract(
        adoption=Adoption.LEGACY,
        profiles=dispositions(F=Claim(gaps={"assert_stopped_work_is_not_revived": WHY}), A=KnownGap(WHY)),
    )


def test_a_new_feature_scheduled_selection_refuses_gaps() -> None:
    with pytest.raises(DueWorkContractDesignError, match="forbids gap declarations"):
        ScheduledSelection(
            name="self-test selection",
            due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
            assert_scheduled=lambda: None,
            gaps={"assert_selection_is_index_served": WHY},
        )


# Extras must reach production, or say why they cannot.


def test_an_extra_that_feeds_a_shared_proof_test_bindings_is_refused() -> None:
    """
    The standalone-proof bypass, closed.

    Every profile binding runs through the invariant-0 guards; the standalone
    proofs take loose callables, so an extra could hand
    ``assert_in_flight_work_is_not_redispatched`` a fabricated ``make_owed``,
    a ``run_tick`` that returns None and a hard-coded dispatch count, and the
    generated case would run green while certifying nothing.
    """
    fabricated: list[int] = []

    def make_owed() -> int:
        fabricated.append(len(fabricated) + 1)
        return fabricated[-1]

    def run_tick() -> None:
        return None

    def run() -> None:
        # ARRANGE — fabricated rows, entirely in this module.
        # REAL PRODUCTION — none, which is the point of this test.
        # EXTERNAL SEAM — none.
        # OBSERVE — a hard-coded count.
        assert_in_flight_work_is_not_redispatched(
            name="fabricated",
            make_owed=make_owed,
            run_tick=run_tick,
            dispatch_count_for=lambda _row: 1,
        )

    with pytest.raises(DueWorkContractDesignError, match="extra proof 'fabricated'.*references no production callable"):
        _contract(extras=(ExtraProof(name="fabricated", run=run),))


def test_an_extra_that_reaches_production_needs_no_declaration(production_host: Host) -> None:
    def run() -> None:
        # ARRANGE — none needed.
        # REAL PRODUCTION — the sample production task the shared proof is about.
        # EXTERNAL SEAM — none.
        # OBSERVE — the shared assertion owns the verdict.
        tasks.cleanup_task()
        assert_self_test_probe_fires([])

    contract = _contract(extras=(ExtraProof(name="reaching", run=run),))
    assert "extra-reaching" in set(_params_by_id(contract_cases(contract)))


def test_a_policy_extra_over_constants_declares_why_it_reaches_no_callable() -> None:
    """A lease-vs-time-limit proof genuinely has no callable; it says so."""
    contract = _contract(
        extras=(ExtraProof(name="policy", run=annotated_extra, no_production_callable_because=SELF_TEST_NO_PRODUCTION),)
    )
    assert "extra-policy" in set(_params_by_id(contract_cases(contract)))


def test_declaring_no_production_callable_while_reaching_one_is_a_contradiction(production_host: Host) -> None:
    """The declaration is not a blanket opt-out: it must describe reality."""

    def run() -> None:
        # ARRANGE — none needed.
        # REAL PRODUCTION — this extra does reach a production module.
        # EXTERNAL SEAM — none.
        # OBSERVE — the shared assertion owns the verdict.
        assert_self_test_probe_fires([])
        assert tasks is not None

    with pytest.raises(DueWorkContractDesignError, match="contradict each other"):
        _contract(
            extras=(ExtraProof(name="contradictory", run=run, no_production_callable_because=SELF_TEST_NO_PRODUCTION),)
        )


# Covered sources.


def test_due_work_source_keeps_the_real_callable_identity() -> None:
    source = DueWorkSource(_reference_due_work_source, sites=2)

    assert source.callable is _reference_due_work_source
    assert source.sites == 2
    assert source.qualified_name == f"{__name__}._reference_due_work_source"


def test_a_suite_refuses_duplicate_due_work_sources() -> None:
    source = DueWorkSource(_reference_due_work_source)

    with pytest.raises(DueWorkContractDesignError, match="duplicate DueWorkSource"):
        due_work_contract_suite(_contract(), covers=(source, source))


def _recovering_contract(**overrides: Any) -> DueWorkContract:
    return _contract(profiles=dispositions(F=Claim(), A=Claim()), sweep=annotated_never_built, **overrides)


def _suite_ids(decorated: type) -> set[str]:
    return {param.id for param in decorated.test_due_work_contract.pytestmark[0].args[1]}  # type: ignore[attr-defined]


def test_a_profile_a_contract_must_say_what_its_covered_publishers_leave_behind() -> None:
    """
    ``covers=`` alone is association without meaning: a contract could name one
    publisher while its sweep recovered a different table, and every generated
    case would still be green.
    """
    with pytest.raises(DueWorkContractDesignError, match="every covered source must say whether this sweep recovers"):
        due_work_contract_suite(_recovering_contract(), covers=(DueWorkSource(tasks.cleanup_task),))(
            type("TCovered", (), {})
        )


def test_a_declared_recovery_split_collects_as_a_visible_row() -> None:
    """A publisher another lifecycle owns is declared, not silently skipped."""
    decorated = due_work_contract_suite(
        _recovering_contract(),
        covers=(
            DueWorkSource(
                tasks.cleanup_task,
                unrecoverable_because="self-test: a different lifecycle demonstrably owns this publisher's work",
            ),
        ),
    )(type("TCoveredDeclined", (), {}))
    assert "covers-cleanup_task-recovery_declined" in _suite_ids(decorated)


def test_a_covered_publisher_generates_its_recovery_case() -> None:
    decorated = due_work_contract_suite(
        _recovering_contract(),
        covers=(DueWorkSource(tasks.reindex_task, publish=lambda: 1, make_recovery_eligible=lambda _identity: None),),
    )(type("TCoveredRecovered", (), {}))
    assert "covers-reindex_task-assert_published_work_is_recoverable" in _suite_ids(decorated)


def test_a_contract_that_does_not_claim_recovery_needs_no_publisher_binding() -> None:
    """Declining profile A leaves no recovery to check the association against."""
    decorated = due_work_contract_suite(_contract(), covers=(DueWorkSource(tasks.cleanup_task),))(
        type("TCoveredNoRecovery", (), {})
    )
    assert not any(case_id.startswith("covers-") for case_id in _suite_ids(decorated))


def test_publish_without_an_ageing_callback_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="without `make_recovery_eligible`"):
        DueWorkSource(tasks.cleanup_task, publish=lambda: 1)


def test_declaring_both_a_publisher_and_a_split_is_a_contradiction() -> None:
    with pytest.raises(DueWorkContractDesignError, match="contradict each other"):
        DueWorkSource(
            tasks.cleanup_task,
            publish=lambda: 1,
            make_recovery_eligible=lambda _identity: None,
            unrecoverable_because="self-test",
        )


# ScheduledSelection schedule completeness.


def test_scheduled_selection_requires_evidence_or_a_declaration() -> None:
    with pytest.raises(DueWorkContractDesignError, match="no schedule evidence and no unscheduled_because"):
        ScheduledSelection(
            name="self-test selection",
            due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
        )
    with pytest.raises(DueWorkContractDesignError, match="contradict each other"):
        ScheduledSelection(
            name="self-test selection",
            due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
            assert_scheduled=lambda: None,
            unscheduled_because=WHY,
        )


def test_a_declared_unscheduled_selection_collects_visibly() -> None:
    selection = ScheduledSelection(
        name="self-test selection",
        due_work=annotated_empty_selection,  # type: ignore[arg-type] - never evaluated
        unscheduled_because="self-test: driven by an external scheduler",
    )
    params = _params_by_id(scheduled_selection_cases(selection))
    assert "schedule_evidence-declined" in params
    params["schedule_evidence-declined"].values[0].run()


# The typed gap probes.


def test_missing_reclaim_fails_while_the_state_is_unreclaimed() -> None:
    probe = MissingReclaim(make_stranded=lambda: 7, dispatched_by_one_tick=lambda: [1, 2])
    with pytest.raises(AssertionError, match="not re-dispatched by the recovery tick"):
        probe()


def test_missing_reclaim_passes_once_the_tick_reclaims() -> None:
    MissingReclaim(make_stranded=lambda: 7, dispatched_by_one_tick=lambda: [7])()


def test_missing_scheduled_consumer_fails_while_nothing_consumes() -> None:
    probe = MissingScheduledConsumer(
        make_stranded=lambda: 7,
        outstanding=lambda: [7],
        scheduled_tasks=lambda: ["app.other.tasks.unrelated"],
        task_name_fragment="self_test_consumer",
    )
    with pytest.raises(AssertionError, match="never recovered"):
        probe()


def test_missing_scheduled_consumer_passes_once_a_consumer_is_scheduled() -> None:
    MissingScheduledConsumer(
        make_stranded=lambda: 7,
        outstanding=lambda: [7],
        scheduled_tasks=lambda: ["app.x.tasks.self_test_consumer_tick"],
        task_name_fragment="self_test_consumer",
    )()


def test_missing_scheduled_consumer_reads_the_schedule_on_every_call() -> None:
    """The day the schedule entry lands, the same probe instance must trip its strict xfail."""
    schedule: list[str] = []
    probe = MissingScheduledConsumer(
        make_stranded=lambda: 7,
        outstanding=lambda: [7],
        scheduled_tasks=lambda: schedule,
        task_name_fragment="self_test_consumer",
    )
    with pytest.raises(AssertionError, match="never recovered"):
        probe()
    schedule.append("app.x.tasks.self_test_consumer_tick")
    probe()


def test_missing_scheduled_consumer_refuses_an_underivable_obligation() -> None:
    """The probe must not conflate 'unconsumed' with 'not derivable at all'."""
    probe = MissingScheduledConsumer(
        make_stranded=lambda: 7,
        outstanding=lambda: [],
        scheduled_tasks=lambda: ["app.x.tasks.anything"],
        task_name_fragment="anything",
    )
    with pytest.raises(AssertionError, match="not in the production outstanding selection"):
        probe()


# DisprovenCapability: root-owned decline evidence.


def test_disproven_capability_accepts_an_honest_edge_triggered_decline() -> None:
    """
    The honest shape: production-bound (root-owned reference) bindings, and the
    discriminating proof fails on exactly the assertion the decline is about.
    """
    DisprovenCapability(
        binding=lambda: materialising_derivation_binding(EdgeTriggeredDeriver()),
        proof=assert_unrecorded_obligation_is_discovered,
        match="was not discovered",
    )()


def test_disproven_capability_rejects_a_synthetic_binding() -> None:
    """
    The sham-decline shape, pinned: a test-authored implementation fed to the
    shared proof "proves" the fake fails, not that production lacks the
    capability. The probe runs the profile's invariant-0 guards first, so the
    synthetic binding is rejected before anything is disproven.
    """

    class _SyntheticDeriver(MaterialisingDeriver):
        def derive(self) -> None:
            return None  # a test-module fake, not production's derivation

    probe = DisprovenCapability(
        binding=lambda: materialising_derivation_binding(_SyntheticDeriver()),
        proof=assert_unrecorded_obligation_is_discovered,
        match="was not discovered",
    )
    with pytest.raises(AssertionError, match="derive.*references no production"):
        probe()


def test_disproven_capability_refutes_a_decline_the_proof_passes() -> None:
    """A capability that exists cannot be declined: the probe demands the flip."""
    probe = DisprovenCapability(
        binding=materialising_derivation_binding,
        proof=assert_unrecorded_obligation_is_discovered,
        match="was not discovered",
    )
    with pytest.raises(AssertionError, match="Flip the disposition to a Claim"):
        probe()


def test_disproven_capability_pins_the_failure_to_its_invariant() -> None:
    """A refutation that fails on the way to its assertion disproves nothing."""
    probe = DisprovenCapability(
        binding=lambda: materialising_derivation_binding(EdgeTriggeredDeriver()),
        proof=assert_unrecorded_obligation_is_discovered,
        match="a message about some other invariant entirely",
    )
    with pytest.raises(AssertionError, match=r"the failure was a\s+different one"):
        probe()


def test_disproven_capability_is_accepted_as_a_decline_prove() -> None:
    """The probe is root-owned, so the bespoke-assertion check accepts it."""
    _contract(
        profiles=dispositions(
            F=Claim(),
            B=Decline(
                WHY,
                prove=DisprovenCapability(
                    binding=lambda: materialising_derivation_binding(EdgeTriggeredDeriver()),
                    proof=assert_unrecorded_obligation_is_discovered,
                    match="was not discovered",
                ),
            ),
        )
    )


# The generated suite, actually running in a child pytest session.

_REPOSITORY = Path(__file__).resolve().parents[3]
_OUTCOME = re.compile(r"(\d+) (passed|failed|xfailed|xpassed|skipped|errors|error)\b")


def _run_reference_cases(mode: str, *selection: str) -> tuple[dict[str, int], str, int]:
    """Run the specimens in a child pytest; return its outcome counts, output and exit code."""
    cases = Path(__file__).with_name("reference_contract_cases.py")
    python_path = os.pathsep.join(
        filter(None, [str(_REPOSITORY / "src"), str(_REPOSITORY), os.environ.get("PYTHONPATH", "")])
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{cases}::TestInMemoryReferenceContract",
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(_REPOSITORY),
            *selection,
        ],
        cwd=_REPOSITORY,
        env={**os.environ, "PYTHONPATH": python_path, "DUE_WORK_REFERENCE_OUTCOME": mode},
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    summary = output.strip().splitlines()[-1] if output.strip() else ""
    outcomes = {noun: int(count) for count, noun in _OUTCOME.findall(summary)}
    return outcomes, output, completed.returncode


def _generated_case_count() -> int:
    assert REFERENCE_CONTRACT.safety is not None
    return len(contract_cases(REFERENCE_CONTRACT)) + len(safety_contract_cases(REFERENCE_CONTRACT.safety))


def test_the_conforming_reference_suite_passes_every_generated_case() -> None:
    outcomes, output, code = _run_reference_cases("conforming")
    assert outcomes == {"passed": _generated_case_count()}, output
    assert code == pytest.ExitCode.OK, output


def test_the_broken_reference_suite_reports_its_gaps_as_strict_xfails() -> None:
    outcomes, output, code = _run_reference_cases("broken", "-k", "known_gap or known-broken-extra")
    assert outcomes == {"xfailed": 2}, output
    assert code == pytest.ExitCode.OK, output


def test_the_repaired_reference_suite_fails_until_the_gaps_are_deleted() -> None:
    """A fixed gap is a strict XPASS: the suite fails until its declaration is deleted."""
    outcomes, output, code = _run_reference_cases("repaired", "-k", "known_gap or known-broken-extra")
    assert outcomes == {"failed": 2}, output
    assert code == pytest.ExitCode.TESTS_FAILED, output
    assert output.count("[XPASS(strict)]") == 2, output


# Handoff histories.


def _handoff(name: str = "retry") -> HandoffHistory[None, None]:
    return HandoffHistory(
        name=name, arrange=lambda: None, transition=lambda _handle: None, observe=lambda _handle: None
    )


_DELIVERY = CallableDelivery(name="self-test delivery", recover=in_memory_handoffs.recover)


def test_handoff_histories_need_a_delivery() -> None:
    with pytest.raises(DueWorkContractDesignError, match="handoff histories are declared without `handoff_delivery=`"):
        _contract(handoffs=(_handoff(),))


def test_a_delivery_without_handoff_histories_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="no handoff history is declared"):
        _contract(handoff_delivery=_DELIVERY)


def test_handoff_history_names_are_unique_and_gaps_name_one() -> None:
    with pytest.raises(DueWorkContractDesignError, match="handoff history names must be unique"):
        _contract(handoffs=(_handoff(), _handoff()), handoff_delivery=_DELIVERY)
    with pytest.raises(DueWorkContractDesignError, match="handoff_gaps name no declared handoff history"):
        _contract(
            handoffs=(_handoff(),), handoff_delivery=_DELIVERY, handoff_gaps={"other": WHY}, adoption=Adoption.LEGACY
        )


def test_a_new_feature_cannot_waive_a_handoff_history() -> None:
    with pytest.raises(DueWorkContractDesignError, match="handoff 'retry' declares a gap"):
        _contract(handoffs=(_handoff(),), handoff_delivery=_DELIVERY, handoff_gaps={"retry": WHY})


def test_each_handoff_history_is_one_transactional_case_and_a_legacy_gap_is_a_strict_xfail(
    marking_host: Host,
) -> None:
    contract = _contract(
        handoffs=(_handoff("retry"), _handoff("fan-out")),
        handoff_delivery=_DELIVERY,
        handoff_gaps={"retry": WHY},
        adoption=Adoption.LEGACY,
    )
    cases = {param.id: param for param in contract_cases(contract) if param.id.startswith("handoff-")}
    assert set(cases) == {
        "handoff-retry-assert_crash_at_every_commit_converges",
        "handoff-fan-out-assert_crash_at_every_commit_converges",
    }
    retry_marks = {mark.name: mark for mark in cases["handoff-retry-assert_crash_at_every_commit_converges"].marks}
    assert retry_marks["database"].kwargs == {"transaction": True}
    assert retry_marks["xfail"].kwargs == {"strict": True, "reason": WHY}
    fan_out_marks = {mark.name for mark in cases["handoff-fan-out-assert_crash_at_every_commit_converges"].marks}
    assert fan_out_marks == {"database"}


def _ledger_handoff_case(transition: Any) -> Any:
    history = HandoffHistory(
        name="retryable failure",
        arrange=in_memory_handoffs.running_attempt,
        transition=transition,
        observe=in_memory_handoffs.attempt_and_successors,
    )
    contract = _contract(handoffs=(history,), handoff_delivery=in_memory_handoffs.RETRY_DELIVERY)
    return _params_by_id(contract_cases(contract))["handoff-retryable failure-assert_crash_at_every_commit_converges"]


def test_a_generated_handoff_case_converges_for_an_atomic_handoff(ledger_host: Host) -> None:
    _ledger_handoff_case(in_memory_handoffs.fail_with_atomic_handoff).values[0].run()


def test_a_generated_handoff_case_fails_a_split_handoff_through_the_contract_delivery(ledger_host: Host) -> None:
    case = _ledger_handoff_case(in_memory_handoffs.fail_with_split_handoff)
    with pytest.raises(AssertionError, match=r"'worker died after commit 1': \('retryable_failed', \(\)\)"):
        case.values[0].run()
