"""
The execution-eligibility proofs, pointed at an independent scheduler we control, in both directions.

The reference scheduler conforms, with and without a periodic fallback. Each of
its ten injected faults is one way a scheduler with blocked work goes wrong, and
each must fail the one proof that owns it, with that proof's message. Both
directions matter: a proof that a conforming scheduler passes and a broken one
also passes would certify nothing.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from due_work_harness.profiles.execution_eligibility import (
    ELIGIBILITY_PROOFS,
    ExecutionGate,
    assert_blocked_gate_preserves_intent,
    assert_eligible_gate_recovers_lost_notification,
    assert_gate_bindings_are_production_bound,
    assert_periodic_inspection_is_bounded,
)
from due_work_harness.references.eligibility import GateReference


@pytest.mark.parametrize("proof", ELIGIBILITY_PROOFS, ids=lambda proof: proof.__name__)
@pytest.mark.parametrize("fallback", [None, timedelta(seconds=30)], ids=["notification-driven", "periodic"])
def test_every_proof_accepts_the_independent_scheduler(
    proof: Callable[[ExecutionGate], None], fallback: timedelta | None
) -> None:
    proof(GateReference(recheck_after=fallback).binding())


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
        ("early_fallback", assert_periodic_inspection_is_bounded, "blocked intent was selected"),
        ("hot_loop", assert_periodic_inspection_is_bounded, "hot-loops"),
    ],
)
def test_each_proof_rejects_the_scheduler_broken_in_its_way(
    fault: str, proof: Callable[[ExecutionGate], None], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        proof(GateReference(fault=fault).binding())


def test_a_failure_names_the_gate() -> None:
    with pytest.raises(AssertionError, match=r"independent in-memory scheduler: execute: blocked work executed"):
        assert_blocked_gate_preserves_intent(GateReference(fault="blocked_execution").binding())


def test_an_example_that_is_already_complete_is_refused() -> None:
    # "Nothing changed" would be true of finished work; the control needs a blocked example.
    reference = GateReference()
    reference.completed = True
    with pytest.raises(AssertionError, match="positive control: example is already complete"):
        assert_blocked_gate_preserves_intent(reference.binding())


def test_the_boundary_is_probed_by_moving_the_clock_never_by_sleeping() -> None:
    reference = GateReference()
    assert_periodic_inspection_is_bounded(reference.binding())
    # Blocked one microsecond short of the boundary, released across it: the injected clock crossed it.
    assert reference.elapsed == timedelta(seconds=30)


@pytest.mark.parametrize(
    "changes",
    [
        {"routes": {}},
        {"routes": {" ": lambda: None}},
        {"recovery_interval": timedelta(0)},
        {"recovery_timeout": timedelta(seconds=1)},
        {"recheck_after": timedelta(0)},
    ],
    ids=["no-routes", "blank-route-name", "zero-interval", "timeout-shorter-than-interval", "zero-recheck"],
)
def test_invalid_gate_bounds_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="ExecutionGate needs|recheck_after must be positive"):
        GateReference().binding().model_copy(update=changes)


@pytest.mark.parametrize("field", ["due_work", "recover", "routes"])
def test_a_test_authored_selection_or_noop_execution_is_refused(field: str) -> None:
    # The reference is harness-owned, so it passes; the same gate with one test-authored binding must not.
    gate = GateReference().binding()
    assert_gate_bindings_are_production_bound(gate)
    replacement: dict[str, Any] = {
        "due_work": {"due_work": lambda: []},
        "recover": {"recover": lambda: None},
        "routes": {"routes": {"execute": lambda: None}},
    }[field]
    with pytest.raises(AssertionError, match="production"):
        assert_gate_bindings_are_production_bound(gate.model_copy(update=replacement))
