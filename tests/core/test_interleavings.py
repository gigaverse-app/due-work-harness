"""Root-owned controls discriminate behavior, rather than certify a reference as an adopter."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import partial

import pytest

from pytest_obligation.interleavings.bindings import (
    EvidenceConfluence,
    EvidenceSession,
    InFlightConvergence,
    InFlightSession,
)
from pytest_obligation.interleavings.engine.catalog import evidence_histories
from pytest_obligation.interleavings.engine.provider import AcceptedProviderRequest, ProviderControl
from pytest_obligation.interleavings.model import (
    Fault,
    History,
    HistoryTrace,
    InterleavingFailure,
    Operation,
)
from pytest_obligation.interleavings.testing.reference import evidence_reference, reference


def scenario(*, forget: bool = False) -> InFlightConvergence[int, str, str]:
    return InFlightConvergence(
        name="root-reference",
        bind=lambda: reference(forget=forget),
        intents=("A", "B", "C"),
        seams=("write",),
        independent=True,
        no_transport_because="Root control has no broker; adapter controls cover notification delivery.",
    )


def evidence_scenario() -> EvidenceConfluence[frozenset[str]]:
    return EvidenceConfluence(
        name="root-evidence",
        bind=evidence_reference,
        facts=("x", "y", "z"),
        batchable=True,
        no_ordered_pair_because="Root control isolates receipt batching rather than database connections.",
        no_retry_because="Reference has no sender; only evidence consumption is modeled here.",
    )


def test_catalog_is_pure_unique_and_has_real_order_variation() -> None:
    declaration = scenario()
    declaration.validate_definition()
    histories = declaration.histories()
    assert len({h.steps for h in histories}) == len(histories)
    assert sum("IF.second-late-completion" in h.families for h in histories) == 6
    assert any("IF.return-to-value" in h.families for h in histories)
    for history in histories:
        declaration.run(history)


def test_late_completion_rejects_lost_repair_debt_and_replays() -> None:
    declaration = scenario(forget=True)
    history = next(h for h in declaration.histories() if h.id == "IF.two-orders/write/late")
    with pytest.raises(InterleavingFailure, match="convergence") as caught:
        declaration.run(history)
    trace = HistoryTrace.model_validate_json(caught.value.__notes__[-1])
    assert trace.invariant == "convergence"
    from pytest_obligation.interleavings.engine.runner import replay_history

    with pytest.raises(InterleavingFailure, match="convergence"):
        replay_history(declaration, trace)


def test_evidence_duplicate_schedules_are_deduplicated_and_preserve_families() -> None:
    histories = evidence_histories(("x", "y"), (), None, False)
    duplicates = [h for h in histories if "EC.duplicates" in h.families]
    assert len(duplicates) == 6
    assert len({h.steps for h in histories}) == len(histories)
    assert any({"EC.control", "EC.permutations"} <= set(h.families) for h in histories)


def test_dependencies_are_preserved_including_duplicates_and_partial_evidence() -> None:
    histories = evidence_histories(("x", "y", "z"), (("x", "y"),), None, False)
    for history in histories:
        seen = set()
        for step in history.steps:
            if step.operation == Operation.EVIDENCE:
                if step.value == "y":
                    assert "x" in seen
                seen.add(step.value)


def test_missing_provider_seam_is_not_a_behavioral_gap() -> None:
    from pytest_obligation.interleavings.model import Fault

    provider = ProviderControl(accept=lambda apply: AcceptedProviderRequest(apply=apply))
    provider.arm("missing", Fault.HOLD)
    with pytest.raises(AssertionError, match="declared provider seam"):
        provider.assert_reached()


def test_gap_policy_refuses_new_features_and_only_matches_named_invariant() -> None:

    from pytest_obligation.interleavings.adapters.integration import run_case, validate
    from pytest_obligation.interleavings.model import KnownFailure, KnownInterleavingFailure

    broken = scenario(forget=True)
    history = next(h for h in broken.histories() if h.id == "IF.two-orders/write/late")
    declared = broken.model_copy(
        update={
            "gaps": {
                history.id: KnownFailure(
                    invariant="convergence", reason="Legacy writer forgets unresolved provider requests."
                )
            }
        }
    )
    with pytest.raises(AssertionError, match="new-feature"):
        validate({declared.name: declared}, legacy=False)
    with pytest.raises(KnownInterleavingFailure):
        run_case(declared, history)
    wrong = declared.model_copy(
        update={
            "gaps": {
                history.id: KnownFailure(
                    invariant="retention", reason="A different failure must not satisfy a declared gap."
                )
            }
        }
    )
    with pytest.raises(InterleavingFailure) as caught:
        run_case(wrong, history)
    assert not isinstance(caught.value, KnownInterleavingFailure)


def test_explorer_finds_fourth_revision_defect_and_replays_without_hypothesis() -> None:
    pytest.importorskip("hypothesis")

    from pytest_obligation.interleavings.engine.runner import replay_history
    from pytest_obligation.interleavings.exploration.hypothesis import explore

    broken = scenario().model_copy(update={"bind": lambda: reference(forget_at=4)})
    for history in broken.histories():
        broken.run(history)
    with pytest.raises(InterleavingFailure) as caught:
        explore(broken, max_examples=30, max_steps=50)
    trace_note = next(note for note in caught.value.__notes__ if note.startswith('{"version":'))
    trace = HistoryTrace.model_validate_json(trace_note)
    assert sum(s.operation == Operation.CHANGE for s in trace.history.steps) >= 3
    with pytest.raises(InterleavingFailure):
        replay_history(broken, trace)


def test_fixed_modules_do_not_import_hypothesis() -> None:
    import subprocess
    import sys

    code = """
import sys
from importlib.abc import MetaPathFinder
class NoHypothesis(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'hypothesis' or fullname.startswith('hypothesis.'):
            raise AssertionError('fixed harness imported optional Hypothesis')
sys.meta_path.insert(0, NoHypothesis())
from pytest_obligation.interleavings.engine.catalog import evidence_histories
from pytest_obligation.interleavings.engine.runner import replay_history
assert evidence_histories(('a', 'b'), (), None, False)
assert 'hypothesis' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_provider_controls_distinguish_acceptance_application_and_caller_return() -> None:
    from functools import partial

    from pytest_obligation.interleavings.model import Fault

    applied: list[str] = []
    provider = ProviderControl(accept=lambda apply: AcceptedProviderRequest(apply=apply))
    for fault in (Fault.HOLD, Fault.LOSE_RESPONSE, Fault.REFUSE):
        provider.arm("write", fault)
        with pytest.raises(TimeoutError):
            provider.invoke("write", fault.value, partial(applied.append, fault.value))
    provider.arm("write", Fault.ACKNOWLEDGE_WITHOUT_APPLYING)

    def perform() -> str:
        applied.append("accepted")
        return "applied"

    reply = provider.invoke("write", "accepted", perform, lambda: "received")
    assert reply == "received"  # The caller sees an ordinary reply; nothing was applied.
    assert applied == ["lose_response"]
    assert sum(provider.calls.values()) == 4
    assert sum(provider.effects.values()) == 1
    assert provider.unapplied_acknowledgements == {("write", "accepted"): 1}
    assert [fault for _, fault in provider.reached] == list(Fault)
    provider.complete(0)
    assert applied == ["lose_response", "hold"]
    assert sum(provider.effects.values()) == 2
    provider.assert_reached()


def test_counterfeit_production_binding_fails_before_behavioral_assertions() -> None:
    from pytest_obligation.interleavings.engine.runner import guard

    def invented_recovery() -> None:
        pass

    with pytest.raises(AssertionError, match="references no production"):
        guard(scenario(), "recover", invented_recovery)


def test_failed_history_exits_its_fresh_environment_and_replay_reenters() -> None:
    from contextlib import contextmanager

    entered: list[int] = []
    exited: list[int] = []

    @contextmanager
    def fresh() -> Iterator[InFlightSession[int, str, str]]:
        run = len(entered)
        entered.append(run)
        try:
            with reference(forget=True) as session:
                yield session
        finally:
            exited.append(run)

    broken: InFlightConvergence[int, str, str] = scenario().model_copy(update={"bind": fresh})
    history = next(h for h in broken.histories() if h.id == "IF.two-orders/write/late")
    for _ in range(2):
        with pytest.raises(InterleavingFailure, match="convergence"):
            broken.run(history)
    assert entered == exited == [0, 1]


def test_replay_rejects_incompatible_versions() -> None:
    from pydantic import ValidationError

    from pytest_obligation.interleavings.engine.runner import replay_history

    declaration = scenario()
    trace = HistoryTrace(scenario=declaration.name, scenario_version=2, history=declaration.histories()[0])
    with pytest.raises(AssertionError, match="incompatible replay"):
        replay_history(declaration, trace)
    with pytest.raises(ValidationError):
        HistoryTrace.model_validate({**trace.model_dump(), "catalog_version": 3})


def test_exploration_respects_step_budget_and_valid_revision_order() -> None:
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings

    from pytest_obligation.interleavings.exploration.hypothesis import histories

    @settings(max_examples=20, deadline=None, database=None)
    @given(histories(scenario(), 15))
    def check(history: History) -> None:
        assert len(history.steps) <= 15
        scenario().run(history)

    check()


def test_batching_enumerates_partitions_and_detects_incremental_evidence_loss() -> None:
    from functools import partial

    from pytest_obligation.interleavings.testing.reference import evidence_reference

    declaration = evidence_scenario()
    histories = declaration.histories()
    assert sum("EC.batching" in history.families for history in histories) == 24
    for history in histories:
        declaration.run(history)
    broken = declaration.model_copy(update={"bind": partial(evidence_reference, forget=True)})
    together = next(history for history in histories if any(len(step.facts) == 3 for step in history.steps))
    broken.run(together)  # All-at-once evidence cannot discriminate this defect.
    with pytest.raises(InterleavingFailure, match="evidence"):
        broken.run(histories[0])


def test_batching_does_not_coalesce_causally_dependent_facts() -> None:
    histories = evidence_histories(("x", "y", "z"), (("x", "y"),), None, False, True)
    for history in histories:
        seen: set[str] = set()
        for step in history.steps:
            names = step.facts if step.operation == Operation.BATCH else (step.value,)
            if step.operation not in (Operation.BATCH, Operation.EVIDENCE):
                continue
            if "y" in names:
                assert "x" in seen
            seen.update(names)


@pytest.mark.parametrize("broken_observation", [False, True], ids=["effects", "outcome"])
def test_evidence_expectation_checks_outcome_and_exact_effects_independently(broken_observation: bool) -> None:
    @contextmanager
    def broken() -> Iterator[EvidenceSession[frozenset[str]]]:
        with evidence_reference() as session:
            if broken_observation:
                session = session.model_copy(update={"observe": lambda: frozenset({"unexpected"})})
            else:
                session = session.model_copy(update={"effects": lambda: {"unintended-send": 1}})
            yield session

    declaration = evidence_scenario().model_copy(update={"bind": broken})
    with pytest.raises(InterleavingFailure) as caught:
        declaration.run(declaration.histories()[0])
    assert caught.value.invariant == ("evidence" if broken_observation else "effects")


def test_evidence_missing_subset_obligation_is_an_authoring_error() -> None:
    @contextmanager
    def missing() -> Iterator[EvidenceSession[frozenset[str]]]:
        with evidence_reference() as session:
            session = session.model_copy(update={"expectations": {}})
            yield session

    declaration = evidence_scenario().model_copy(update={"bind": missing})
    with pytest.raises(AssertionError, match="missing independent evidence expectation") as caught:
        declaration.run(declaration.histories()[0])
    assert not isinstance(caught.value, InterleavingFailure)


def test_explored_batches_preserve_prerequisites_budget_and_outcomes() -> None:
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings

    from pytest_obligation.interleavings.exploration.hypothesis import histories

    declaration = evidence_scenario().model_copy(update={"dependencies": (("x", "y"),)})

    @settings(max_examples=40, deadline=None, database=None, derandomize=True)
    @given(histories(declaration, 15))
    def check(history: History) -> None:
        assert len(history.steps) <= 15
        seen: set[str] = set()
        for item in history.steps:
            if item.operation not in (Operation.EVIDENCE, Operation.BATCH):
                continue
            delivered = item.facts if item.operation == Operation.BATCH else (item.value,)
            if "y" in delivered:
                assert "x" in seen  # Earlier consumption, not merely present in the same batch.
            seen.update(delivered)
        assert seen == {"x", "y", "z"}
        declaration.run(history)

    check()


def test_evidence_dependency_cycles_fail_at_collection() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="dependency cycle"):
        evidence_scenario().model_copy(update={"dependencies": (("x", "y"), ("y", "x"))})


@pytest.mark.parametrize("corrupt_revision", [False, True], ids=["acknowledgement", "desired-revision"])
def test_quiet_recovery_preserves_revision_identity_as_well_as_payload(corrupt_revision: bool) -> None:
    declaration = scenario().model_copy(
        update={
            "bind": lambda: reference(
                forget=True, quiet_corruption="desired-revision" if corrupt_revision else "acknowledgement"
            )
        }
    )
    with pytest.raises(InterleavingFailure, match="settled-state"):
        declaration.run(declaration.histories()[0])


@pytest.mark.parametrize("after_retry", [False, True], ids=["initial", "after-retry"])
def test_missing_later_expectation_fails_before_an_earlier_behavioral_gap(after_retry: bool) -> None:
    from pytest_obligation.interleavings.bindings import EvidenceRetry

    prepared: list[bool] = []

    @contextmanager
    def incomplete() -> Iterator[EvidenceSession[frozenset[str]]]:
        with evidence_reference(forget=True) as session:
            session = session.model_copy(update={"prepare": lambda: prepared.append(True)})
            incomplete_outcomes = {
                key: value for key, value in session.expectations.items() if key != frozenset({"x", "y", "z"})
            }
            if after_retry:
                session = session.model_copy(
                    update={"retry": EvidenceRetry(send=lambda replay: None, expectations=incomplete_outcomes)}
                )
                session = session.model_copy(update={"settled_evidence": lambda: "original"})
            else:
                session = session.model_copy(update={"expectations": incomplete_outcomes})
            yield session

    declaration = evidence_scenario().model_copy(update={"bind": incomplete, "retry_turnover": after_retry})
    with pytest.raises(AssertionError, match="missing independent evidence expectation") as caught:
        declaration.run(declaration.histories()[0])
    assert not isinstance(caught.value, InterleavingFailure)
    assert prepared == []


@pytest.mark.parametrize("fails_on_exit", [False, True], ids=["action", "scope-cleanup"])
def test_ordered_actors_stop_after_failed_action_or_scope_cleanup(fails_on_exit: bool) -> None:
    from pytest_obligation.interleavings.engine.runner import ordered_actors

    called: list[str] = []

    @contextmanager
    def scope() -> Iterator[None]:
        try:
            yield
        finally:
            if fails_on_exit:
                raise RuntimeError("actor failed")

    def first() -> None:
        called.append("first")
        if not fails_on_exit:
            raise RuntimeError("actor failed")

    with pytest.raises(RuntimeError, match="actor failed"):
        ordered_actors(first, lambda: called.append("second"), scope)
    assert called == ["first"]


def test_ordered_actors_inherit_but_do_not_leak_execution_context() -> None:
    from contextlib import nullcontext
    from contextvars import ContextVar
    from threading import current_thread

    from pytest_obligation.interleavings.engine.runner import ordered_actors

    universe = ContextVar("interleaving-test-universe", default="missing")
    caller = current_thread()
    observed: list[str] = []
    token = universe.set("adopter")

    def actor() -> None:
        assert current_thread() is not caller
        observed.append(universe.get())
        universe.set("actor-local")

    try:
        ordered_actors(actor, actor, nullcontext)
        assert observed == ["adopter", "adopter"]
        assert universe.get() == "adopter"
    finally:
        universe.reset(token)


@pytest.mark.parametrize("count", [-1, True, "1"])
def test_evidence_effect_counts_are_validated_at_construction(count: object) -> None:
    from pydantic import ValidationError

    from pytest_obligation.interleavings.bindings import EvidenceExpectation

    with pytest.raises(ValidationError):
        EvidenceExpectation.model_validate({"observation": "ok", "effects": {"recipient": count}})


@pytest.mark.parametrize("facts", [("", "y"), ("x,y", "z"), ("x", "x")])
def test_invalid_fact_aliases_are_rejected_before_collection(facts: tuple[str, str]) -> None:
    from pydantic import ValidationError

    declaration = evidence_scenario()
    with pytest.raises(ValidationError):
        EvidenceConfluence.model_validate({**declaration.model_dump(), "facts": facts})


def test_declarations_validate_without_entering_the_binding() -> None:
    from pydantic import ValidationError

    def must_not_bind() -> None:
        raise AssertionError("collection entered the application environment")

    data = {**evidence_scenario().model_dump(), "bind": must_not_bind}
    EvidenceConfluence.model_validate(data)
    for invalid in ({"version": 0}, {"typo_capability": True}, {"dependencies": (("x", "y"), ("y", "x"))}):
        with pytest.raises(ValidationError):
            EvidenceConfluence.model_validate({**data, **invalid})


def test_intents_preserve_canonical_domain_objects() -> None:
    from pytest_obligation.interleavings.bindings import Intent

    canonical = object()
    intent = Intent(value=canonical, expected="ready")
    assert intent.value is canonical


@pytest.mark.parametrize("in_flight", [False, True], ids=["evidence", "in-flight"])
def test_session_wiring_rejects_noncallable_commands_and_unknown_fields(in_flight: bool) -> None:
    from pydantic import ValidationError

    with reference() if in_flight else evidence_reference() as session:
        # Reflection is deliberate here: validate each concrete model's public schema.
        values = {name: getattr(session, name) for name in type(session).model_fields}
        with pytest.raises(ValidationError, match="callable"):
            type(session).model_validate({**values, "recover": None})
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(session).model_validate({**values, "recvoer": session.recover})
        with pytest.raises(ValidationError, match="frozen_instance"):
            session.recover = lambda: None  # type: ignore[read-only] - exercise runtime validation


def test_validated_session_preserves_live_controller_identity_and_isolates_histories() -> None:
    with reference() as first, reference() as second:
        values = {name: getattr(first, name) for name in type(first).model_fields}
        validated = type(first).model_validate(values)
        assert validated.provider is first.provider
        assert validated.provider is not second.provider
        validated.provider.arm("write", Fault.REFUSE)
        handle = validated.admit("A")
        validated.start(handle)
        assert first.provider.calls[("write", str(handle))] == 1
        assert not second.provider.calls and not second.provider.armed


@pytest.mark.parametrize("retry", [False, True], ids=["arrival", "retry"])
def test_evidence_components_validate_callbacks_without_invoking_them(retry: bool) -> None:
    from pydantic import ValidationError

    from pytest_obligation.interleavings import EvidenceArrival, EvidenceRetry

    invoked: list[bool] = []
    if retry:
        component = EvidenceRetry(send=lambda replay: invoked.append(True), expectations={})
        with pytest.raises(ValidationError, match="callable"):
            EvidenceRetry.model_validate({"send": None, "expectations": {}})
    else:
        component = EvidenceArrival(publish=lambda: invoked.append(True), consume=lambda: None)
        with pytest.raises(ValidationError, match="callable"):
            EvidenceArrival.model_validate({"publish": None, "consume": component.consume})
    assert not invoked


def test_binding_cleanup_cannot_turn_a_failed_history_into_a_pass() -> None:
    from contextlib import suppress

    @contextmanager
    def swallowing() -> Iterator[InFlightSession[int, str, str]]:
        with suppress(InterleavingFailure), reference(forget=True) as session:
            yield session

    broken = scenario().model_copy(update={"bind": swallowing})
    history = next(h for h in broken.histories() if h.id == "IF.two-orders/write/late")
    with pytest.raises(InterleavingFailure, match="convergence") as caught:
        broken.run(history)
    trace = HistoryTrace.model_validate_json(caught.value.__notes__[-1])
    assert trace.history == history


# Acknowledgement without application is opt-in per seam. These digests are the
# catalogs generated on main before the feature (f4de472); an unchanged
# declaration must keep producing exactly them.
PRE_FEATURE_CATALOGS = {
    "revisions": (
        (("A", "B", "C"), ("write", "repair"), False, True, True, ()),
        44,
        "8f68f54de20bcc81f9108ddc3fab464dd935ed984fe66ecf7f75a99ea146d33b",
    ),
    "two-revisions": (
        (("A", "B"), ("write",), False, False, False, ()),
        6,
        "317000f13e8a86d8fb7e3d021153477746d3824067198d5a7314dcbf273c2196",
    ),
    "retirement": (
        (("A", "B", "C"), ("write", "repair"), True, True, True, ("repair",)),
        19,
        "8197059326362551cef3dd118427cc532fdade6b3b4f5d6df42be313a3dc14b1",
    ),
    "retirement-plain": (
        (("A",), ("write",), True, False, False, ()),
        5,
        "2291dd486d00d53925bdf6c13e396b7954c1604e00df35ba40afe287e2903943",
    ),
}


def catalog_digest(histories: tuple[History, ...]) -> str:
    import hashlib
    import json

    blob = json.dumps([history.model_dump(mode="json") for history in histories], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def receipt_scenario(
    bind: Callable[[], AbstractContextManager[InFlightSession[int, str, str]]], *, replay_safe: bool = True
) -> InFlightConvergence[int, str, str]:
    return scenario().model_copy(
        update={"bind": bind, "acknowledgement_only_seams": ("write",), "replay_safe": replay_safe}
    )


def ack_without_apply(declaration: InFlightConvergence[int, str, str]) -> History:
    return next(h for h in declaration.histories() if "IF.ack-without-apply" in h.families)


@pytest.mark.parametrize("name", sorted(PRE_FEATURE_CATALOGS))
@pytest.mark.parametrize("replay_safe", [True, False], ids=["replay-safe", "replay-unsafe"])
def test_a_declaration_that_does_not_opt_in_keeps_its_pre_feature_catalog(name: str, replay_safe: bool) -> None:
    from pytest_obligation.interleavings.engine.catalog import in_flight_histories

    (intents, seams, retirement, independent, transport, repair), count, digest = PRE_FEATURE_CATALOGS[name]
    histories = in_flight_histories(intents, seams, retirement, independent, transport, repair, replay_safe=replay_safe)
    assert (len(histories), catalog_digest(histories)) == (count, digest)
    declaration = InFlightConvergence(
        name=name,
        bind=reference,
        intents=intents,
        seams=seams,
        repair_seams=repair,
        retirement=retirement,
        independent=independent,
        transport=transport,
        replay_safe=replay_safe,
        limited_revisions_because="Collection-only declaration.",
        no_independent_because="Collection-only declaration.",
        no_transport_because="Collection-only declaration.",
        no_repair_because="Collection-only declaration.",
    )
    assert catalog_digest(declaration.histories()) == digest
    assert "acknowledgement without application" not in declaration.limitations()


def test_only_the_opted_in_seam_of_two_gets_acknowledgement_histories() -> None:
    declaration = scenario().model_copy(
        update={
            "seams": ("send_billing_event", "store_snapshot"),
            "acknowledgement_only_seams": ("send_billing_event",),
        }
    )
    unchanged = scenario().model_copy(update={"seams": ("send_billing_event", "store_snapshot")})
    added = [h for h in declaration.histories() if h not in unchanged.histories()]
    assert [h.id for h in added] == [
        "IF.ack-without-apply/send_billing_event",
        "IF.ack-without-apply/send_billing_event/update",
        "IF.ack-without-apply/send_billing_event/return-to-value",
    ]
    assert all(
        s.seam == "send_billing_event" for h in added for s in h.steps if s.fault == Fault.ACKNOWLEDGE_WITHOUT_APPLYING
    )
    retirement = declaration.model_copy(
        update={"retirement": True, "replay_safe": False, "no_repair_because": "Collection-only declaration."}
    )
    assert not any("IF.ack-without-apply" in h.families for h in retirement.histories())
    assert "acknowledgement without application" in retirement.limitations()


def test_acknowledgement_only_seams_must_be_declared_seams() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="undeclared seams: \\['send_billing_event'\\]"):
        scenario().model_copy(update={"acknowledgement_only_seams": ("send_billing_event",)})


def test_the_fault_cannot_be_armed_at_a_seam_that_guarantees_completion() -> None:
    declaration = scenario()
    history = ack_without_apply(receipt_scenario(reference))
    with pytest.raises(AssertionError, match="write is not declared acknowledgement-only"):
        declaration.run(history)


def test_a_writer_trusting_a_completion_guaranteeing_api_passes_without_read_back() -> None:
    declaration = scenario()  # Confirms from the reply; nothing is opted in.
    histories = declaration.histories()
    assert not any(s.fault == Fault.ACKNOWLEDGE_WITHOUT_APPLYING for h in histories for s in h.steps)
    for history in histories:
        declaration.run(history)


def test_a_receipt_only_writer_confirms_from_provider_state_across_the_whole_catalog() -> None:
    declaration = receipt_scenario(partial(reference, receipt_only=True))
    history = ack_without_apply(declaration)
    assert history.id == "IF.ack-without-apply/write"
    for each in declaration.histories():
        declaration.run(each)


@contextmanager
def synchronously_verified_writer() -> Iterator[InFlightSession[int, str, str]]:
    with reference(receipt_only=True) as session:
        assert session.desired_identity is not None
        assert session.acknowledged_identity is not None
        desired_identity = session.desired_identity
        acknowledged_identity = session.acknowledged_identity

        def start(handle: int) -> None:
            session.start(handle)
            if acknowledged_identity(handle) != desired_identity(handle):
                # Retry through the same writer: confirmation still requires
                # its independent provider-applied revision, never the receipt.
                session.start(handle)

        yield session.model_copy(update={"start": start})


@pytest.mark.parametrize("replay_safe", [True, False], ids=["replay-safe", "replay-unsafe"])
def test_synchronous_verified_confirmation_respects_replay_safety(replay_safe: bool) -> None:
    declaration = receipt_scenario(synchronously_verified_writer, replay_safe=replay_safe)
    history = ack_without_apply(declaration)
    if replay_safe:
        # Application and read-back finish before START returns. The receipt
        # checkpoint must accept a revision that really was applied and verified.
        declaration.run(history)
    else:
        # Verified application must not excuse repeating a non-repeatable call.
        with pytest.raises(InterleavingFailure) as caught:
            declaration.run(history)
        assert caught.value.invariant == "non-repeatable"


def test_an_opted_in_writer_that_confirms_from_the_receipt_fails() -> None:
    from pytest_obligation.interleavings.engine.runner import replay_history

    declaration = receipt_scenario(partial(reference, forget=True))
    history = ack_without_apply(declaration)
    with pytest.raises(InterleavingFailure, match="only acknowledged receipt and never applied it") as caught:
        declaration.run(history)
    assert caught.value.invariant == "acknowledged-not-applied"
    trace = HistoryTrace.model_validate_json(caught.value.__notes__[-1])
    with pytest.raises(InterleavingFailure, match="acknowledged-not-applied"):
        replay_history(declaration, trace)


def test_confirming_early_from_the_receipt_fails_even_when_recovery_later_repairs_it() -> None:
    declaration = receipt_scenario(reference)  # Confirms from the reply; recovery re-reads the provider.
    history = ack_without_apply(declaration)
    with pytest.raises(InterleavingFailure) as caught:
        declaration.run(history)
    assert caught.value.invariant == "acknowledged-not-applied"
    assert HistoryTrace.model_validate_json(caught.value.__notes__[-1]).completed_steps == 3
    # Control: without the checkpoint before recovery, the repair hides it.
    late_only = history.model_copy(
        update={"steps": tuple(s for s in history.steps if s.operation != Operation.UNCONFIRMED)}
    )
    declaration.run(late_only)


def test_without_replay_an_unapplied_receipt_must_stay_unconfirmed() -> None:
    declaration = receipt_scenario(partial(reference, replay_safe=False, receipt_only=True), replay_safe=False)
    history = ack_without_apply(declaration)
    assert history.id == "IF.ack-without-apply/write/no-replay"
    declaration.run(history)  # Never re-sends, never confirms from the receipt.
    trusting = declaration.model_copy(update={"bind": partial(reference, replay_safe=False)})
    with pytest.raises(InterleavingFailure) as trusted:
        trusting.run(history)
    assert trusted.value.invariant == "acknowledged-not-applied"
    # Confirming by sending again is the other way out, and it is refused too.
    resending = declaration.model_copy(update={"bind": partial(reference, receipt_only=True)})
    with pytest.raises(InterleavingFailure) as resent:
        resending.run(history)
    assert resent.value.invariant == "non-repeatable"


@pytest.mark.parametrize("explicit_recovery", [False, True], ids=["settle-loop", "explicit-recover"])
@pytest.mark.parametrize("replay_safe", [True, False], ids=["replay-safe", "replay-unsafe"])
def test_receipt_confirmation_is_rejected_on_each_recovery_turn(explicit_recovery: bool, replay_safe: bool) -> None:
    from pytest_obligation.interleavings.engine.catalog import step
    from pytest_obligation.interleavings.engine.runner import replay_history

    declaration = receipt_scenario(
        partial(reference, receipt_only=True, replay_safe=replay_safe, confirm_receipt_during_recovery=True),
        replay_safe=replay_safe,
    )
    history = ack_without_apply(declaration)
    if explicit_recovery:
        steps = list(history.steps)
        steps.insert(4, step(Operation.RECOVER))
        history = history.model_copy(update={"steps": tuple(steps)})
    correct = receipt_scenario(partial(reference, receipt_only=True, replay_safe=replay_safe), replay_safe=replay_safe)
    correct.run(history)
    with pytest.raises(InterleavingFailure) as caught:
        declaration.run(history)
    assert caught.value.invariant == "acknowledged-not-applied"
    trace = HistoryTrace.model_validate_json(caught.value.__notes__[-1])
    assert trace.completed_steps == 4
    with pytest.raises(InterleavingFailure, match="acknowledged-not-applied"):
        replay_history(declaration, trace)


@pytest.mark.parametrize("replay_safe", [True, False], ids=["replay-safe", "replay-unsafe"])
@pytest.mark.parametrize("first_bad_revision", [2, 3], ids=["updates", "return-to-value"])
def test_receipt_histories_cover_updates_and_return_to_previous_values(
    replay_safe: bool, first_bad_revision: int
) -> None:
    from pytest_obligation.interleavings.engine.runner import replay_history

    correct = receipt_scenario(partial(reference, receipt_only=True, replay_safe=replay_safe), replay_safe=replay_safe)
    histories = [h for h in correct.histories() if "IF.ack-without-apply" in h.families]
    assert len(histories) == 3  # Initial write, update, and A -> B -> A.
    broken = correct.model_copy(
        update={
            "bind": partial(
                reference, receipt_only=True, replay_safe=replay_safe, trust_receipts_from_revision=first_bad_revision
            )
        }
    )
    for history in histories:
        correct.run(history)
    for history in histories[: first_bad_revision - 1]:
        broken.run(history)  # Earlier revisions still verify the provider.
    for history in histories[first_bad_revision - 1 :]:
        with pytest.raises(InterleavingFailure) as caught:
            broken.run(history)
        assert caught.value.invariant == "acknowledged-not-applied"
        trace = HistoryTrace.model_validate_json(caught.value.__notes__[-1])
        with pytest.raises(InterleavingFailure, match="acknowledged-not-applied"):
            replay_history(broken, trace)


def test_unconfirmed_receipts_cannot_erase_the_commanded_revision_during_recovery() -> None:
    declaration = receipt_scenario(
        partial(reference, receipt_only=True, replay_safe=False, quiet_corruption="desired-revision"),
        replay_safe=False,
    )
    history = ack_without_apply(declaration)
    correct = receipt_scenario(partial(reference, receipt_only=True, replay_safe=False), replay_safe=False)
    correct.run(history)
    with pytest.raises(InterleavingFailure) as caught:
        declaration.run(history)
    assert caught.value.invariant == "revision"
