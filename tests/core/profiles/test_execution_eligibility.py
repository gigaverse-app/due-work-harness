"""
The execution-eligibility proofs, pointed at an independent scheduler we control, in both directions.

The reference scheduler conforms: with and without a periodic fallback, with
inspections counted as executions or observed on their own, and with a clock
that moves only in whole seconds once that resolution is declared. Each
injected fault is one way a scheduler with blocked work goes wrong, and each
must fail with the message of the check that catches it. Proofs 3 and 4 begin
by re-running proof 2, so a blocked-work fault fails them too; the table names
the first proof that owns each fault. Both directions matter: a proof that a
conforming scheduler passes and a broken one also passes would certify nothing.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from due_work_harness.models import DueWorkContractDesignError
from due_work_harness.profiles.execution_eligibility import (
    ELIGIBILITY_PROOFS,
    ExecutionGate,
    assert_blocked_gate_preserves_intent,
    assert_eligible_gate_recovers_lost_notification,
    assert_gate_bindings_are_production_bound,
    assert_gate_is_recovered_by_the_contract_sweep,
    assert_periodic_inspection_is_bounded,
)
from due_work_harness.references.eligibility import GateReference


@pytest.mark.parametrize("proof", ELIGIBILITY_PROOFS, ids=lambda proof: proof.__name__)
@pytest.mark.parametrize(
    "reference",
    [
        GateReference(recheck_after=None),
        GateReference(),
        GateReference(separate_inspections=True),
        GateReference(whole_seconds=True, clock_resolution=timedelta(seconds=1)),
    ],
    ids=["notification-driven", "periodic", "inspections-observed-apart", "whole-second-clock"],
)
def test_every_proof_accepts_the_independent_scheduler(
    proof: Callable[[ExecutionGate], None], reference: GateReference
) -> None:
    proof(reference.model_copy(deep=True).binding())


@pytest.mark.parametrize(
    ("fault", "proof", "message"),
    [
        ("hidden_write", assert_blocked_gate_preserves_intent, "blocked work mutated durable state"),
        ("lost_intent", assert_blocked_gate_preserves_intent, "intent was lost"),
        ("mutated_intent", assert_blocked_gate_preserves_intent, "mutated reserved state"),
        ("blocked_execution", assert_blocked_gate_preserves_intent, "blocked work executed"),
        ("reset_on_readiness", assert_eligible_gate_recovers_lost_notification, "readiness rewrote reserved intent"),
        ("no_recovery", assert_eligible_gate_recovers_lost_notification, "did not reach"),
        ("fake_completion", assert_eligible_gate_recovers_lost_notification, "never reached the provider"),
        ("no_fallback", assert_periodic_inspection_is_bounded, "never becomes due"),
        ("early_fallback", assert_periodic_inspection_is_bounded, "due before recheck_after has passed"),
        ("hot_loop", assert_periodic_inspection_is_bounded, "hot-loops"),
        # The fallback runs the blocked work: it calls the provider, or completes it, or drops it.
        ("runs_at_inspection", assert_periodic_inspection_is_bounded, "inspection called the provider"),
        ("completes_at_inspection", assert_periodic_inspection_is_bounded, "inspection changed the product state"),
        ("drops_at_inspection", assert_periodic_inspection_is_bounded, "inspection dropped the blocked obligation"),
        # Re-armed one second later, not recheck_after: a hot loop at a one-second sweep cadence.
        ("short_rearm", assert_periodic_inspection_is_bounded, "due before recheck_after has passed"),
    ],
)
def test_each_fault_fails_with_the_message_of_the_check_that_catches_it(
    fault: str, proof: Callable[[ExecutionGate], None], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        proof(GateReference(fault=fault).binding())


def test_an_inspection_observed_apart_must_not_execute_the_blocked_work() -> None:
    reference = GateReference(fault="executes_at_inspection", separate_inspections=True)
    with pytest.raises(AssertionError, match="inspection executed the blocked work"):
        assert_periodic_inspection_is_bounded(reference.binding())


def test_an_inspection_that_is_not_counted_anywhere_is_never_seen() -> None:
    # Observed apart but not declared: the inspection moves no count the proof reads.
    gate = GateReference(separate_inspections=True).binding().model_copy(update={"inspections": None})
    with pytest.raises(AssertionError, match="periodic inspection never inspected"):
        assert_periodic_inspection_is_bounded(gate)


def test_a_coarse_clock_needs_its_resolution_declared() -> None:
    # A whole-second clock never moves by a microsecond: the boundary is never crossed.
    with pytest.raises(AssertionError, match="never becomes due"):
        assert_periodic_inspection_is_bounded(GateReference(whole_seconds=True).binding())


def test_a_failure_names_the_gate() -> None:
    with pytest.raises(AssertionError, match=r"independent in-memory scheduler: execute: blocked work executed"):
        assert_blocked_gate_preserves_intent(GateReference(fault="blocked_execution").binding())


def test_an_example_that_is_already_complete_is_refused() -> None:
    # "Nothing changed" would be true of finished work; the control needs a blocked example.
    reference = GateReference()
    reference.completed = True
    with pytest.raises(AssertionError, match="positive control: example is already complete"):
        assert_blocked_gate_preserves_intent(reference.binding())


def test_the_boundary_is_probed_twice_by_moving_the_clock_never_by_sleeping() -> None:
    reference = GateReference()
    assert_periodic_inspection_is_bounded(reference.binding())
    # Admission to the first inspection, then that inspection to the next: two windows, each crossed.
    assert reference.elapsed == timedelta(seconds=60)


@pytest.mark.parametrize(
    "changes",
    [
        {"routes": {}},
        {"routes": {" ": lambda: None}},
        {"recovery_interval": timedelta(0)},
        {"recovery_timeout": timedelta(seconds=1)},
        {"recheck_after": timedelta(0)},
        {"clock_resolution": timedelta(0)},
        {"clock_resolution": timedelta(seconds=30)},
    ],
    ids=[
        "no-routes",
        "blank-route-name",
        "zero-interval",
        "timeout-shorter-than-interval",
        "zero-recheck",
        "zero-resolution",
        "resolution-not-below-recheck",
    ],
)
def test_invalid_gate_bounds_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(DueWorkContractDesignError, match="ExecutionGate"):
        GateReference().binding().model_copy(update=changes)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("due_work", {"due_work": lambda: []}),
        ("recover", {"recover": lambda: None}),
        ("execute", {"routes": {"execute": lambda: None}}),
        # A test-written owed_work always reports the obligation, hiding lost_intent.
        ("owed_work", {"owed_work": lambda: ["obligation"]}),
        # A test-written readiness flips a flag, hiding what production's transition rewrites.
        ("make_eligible", {"make_eligible": lambda: None}),
    ],
    ids=["due_work", "recover", "routes", "owed_work", "make_eligible"],
)
def test_a_test_authored_binding_is_refused(field: str, replacement: dict[str, Any]) -> None:
    # The reference is harness-owned, so it passes; the same gate with one test-authored binding must not.
    gate = GateReference().binding()
    assert_gate_bindings_are_production_bound(gate)
    with pytest.raises(AssertionError, match=rf"{field}.*references no production"):
        assert_gate_bindings_are_production_bound(gate.model_copy(update=replacement))


# The gate and the contract's sweep describe the same recovery.


def test_a_gate_recovered_by_the_contract_sweep_passes() -> None:
    reference = GateReference()
    assert_gate_is_recovered_by_the_contract_sweep(reference.binding(), reference.sweep())


def test_a_gate_whose_recovery_never_reaches_the_sweeps_tick_is_refused() -> None:
    reference = GateReference()
    # Recovery bound to something the contract's tick never runs: a directly invoked worker, say.
    gate = reference.binding().model_copy(update={"recover": reference.complete_directly})
    with pytest.raises(AssertionError, match="recover never reached the contract sweep's tick"):
        assert_gate_is_recovered_by_the_contract_sweep(gate, reference.sweep())


def test_a_gate_whose_selection_is_not_the_sweeps_is_refused() -> None:
    reference = GateReference()
    other = GateReference(recheck_after=None)
    with pytest.raises(AssertionError, match="eligible work is absent from the contract sweep's selection"):
        assert_gate_is_recovered_by_the_contract_sweep(reference.binding(), other.sweep())


def test_a_sweep_that_selects_blocked_work_is_refused() -> None:
    reference = GateReference()
    released = GateReference(eligible=True)
    with pytest.raises(AssertionError, match="blocked work is in the contract sweep's selection"):
        assert_gate_is_recovered_by_the_contract_sweep(reference.binding(), released.sweep())
