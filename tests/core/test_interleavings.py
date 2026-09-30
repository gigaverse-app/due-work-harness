"""Root-owned controls discriminate behavior, rather than certify a reference as an adopter."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from due_work_harness.interleavings.bindings import (
    EvidenceConfluence,
    EvidenceSession,
    InFlightConvergence,
    InFlightSession,
)
from due_work_harness.interleavings.engine.catalog import evidence_histories
from due_work_harness.interleavings.engine.provider import AcceptedProviderRequest, ProviderControl
from due_work_harness.interleavings.model import (
    Fault,
    History,
    HistoryTrace,
    InterleavingFailure,
    Operation,
)
from due_work_harness.interleavings.testing.reference import evidence_reference, reference


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
    from due_work_harness.interleavings.engine.runner import replay_history

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
    from due_work_harness.interleavings.model import Fault

    provider = ProviderControl(accept=lambda apply: AcceptedProviderRequest(apply=apply))
    provider.arm("missing", Fault.HOLD)
    with pytest.raises(AssertionError, match="declared provider seam"):
        provider.assert_reached()


def test_gap_policy_refuses_new_features_and_only_matches_named_invariant() -> None:

    from due_work_harness.interleavings.adapters.integration import run_case, validate
    from due_work_harness.interleavings.model import KnownFailure, KnownInterleavingFailure

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

    from due_work_harness.interleavings.engine.runner import replay_history
    from due_work_harness.interleavings.exploration.hypothesis import explore

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
from due_work_harness.interleavings.engine.catalog import evidence_histories
from due_work_harness.interleavings.engine.runner import replay_history
assert evidence_histories(('a', 'b'), (), None, False)
assert 'hypothesis' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_provider_controls_distinguish_acceptance_application_and_caller_return() -> None:
    from functools import partial

    from due_work_harness.interleavings.model import Fault

    applied: list[str] = []
    provider = ProviderControl(accept=lambda apply: AcceptedProviderRequest(apply=apply))
    for fault in Fault:
        provider.arm("write", fault)
        with pytest.raises(TimeoutError):
            provider.invoke("write", fault.value, partial(applied.append, fault.value))
    assert applied == ["lose_response"]
    assert sum(provider.calls.values()) == 3
    assert sum(provider.effects.values()) == 1
    provider.complete(0)
    assert applied == ["lose_response", "hold"]
    assert sum(provider.effects.values()) == 2
    provider.assert_reached()


def test_counterfeit_production_binding_fails_before_behavioral_assertions() -> None:
    from due_work_harness.interleavings.engine.runner import guard

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

    from due_work_harness.interleavings.engine.runner import replay_history

    declaration = scenario()
    trace = HistoryTrace(scenario=declaration.name, scenario_version=2, history=declaration.histories()[0])
    with pytest.raises(AssertionError, match="incompatible replay"):
        replay_history(declaration, trace)
    with pytest.raises(ValidationError):
        HistoryTrace.model_validate({**trace.model_dump(), "catalog_version": 3})


def test_exploration_respects_step_budget_and_valid_revision_order() -> None:
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings

    from due_work_harness.interleavings.exploration.hypothesis import histories

    @settings(max_examples=20, deadline=None, database=None)
    @given(histories(scenario(), 15))
    def check(history: History) -> None:
        assert len(history.steps) <= 15
        scenario().run(history)

    check()


def test_batching_enumerates_partitions_and_detects_incremental_evidence_loss() -> None:
    from functools import partial

    from due_work_harness.interleavings.testing.reference import evidence_reference

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

    from due_work_harness.interleavings.exploration.hypothesis import histories

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
    from due_work_harness.interleavings.bindings import EvidenceRetry

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
    from due_work_harness.interleavings.engine.runner import ordered_actors

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

    from due_work_harness.interleavings.engine.runner import ordered_actors

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

    from due_work_harness.interleavings.bindings import EvidenceExpectation

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
    from due_work_harness.interleavings.bindings import Intent

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

    from due_work_harness.interleavings import EvidenceArrival, EvidenceRetry

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
