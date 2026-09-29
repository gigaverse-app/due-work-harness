"""
Execution eligibility: work that is owed but may not run yet is neither lost nor run early.

A due obligation can be *blocked* by something the product decides, not by the
clock: a dependency has not settled, an owner is still active, a user has not
confirmed. The failure modes are the mirror image of a lost handoff, and each
looks fine in normal operation:

* the blocked work is **dropped**, so readiness later finds nothing to release;
* a blocked attempt **executes anyway**, calls the provider, or rewrites its
  own retry state on every sweep (a hidden write no product observation shows);
* becoming eligible **rewrites the obligation** — a new revision, a reset retry
  budget, an earlier not-before time — so a release admits work nobody asked for;
* the readiness notification is lost, and **nothing else ever looks again**;
* the fallback that *does* look again fires too early, never, or in a **hot
  loop** because inspecting the blocked work does not move its next inspection.

:class:`ExecutionGate` describes one blocked, already time-due obligation with
its readiness notification lost. The bindings forward selection and execution
to production and expose observations; these proofs own the verdict. They know
no table, queue, lease or clock implementation, so a native lifecycle and a
work-table adapter bind the same contract. The clock is the adopter's
``advance``: the proofs move it across both sides of the declared boundary and
never sleep.

Declared on :class:`~due_work_harness.contract.DueWorkContract` as
``eligibility=`` (one gate, or named gates for named product blockers), which
generates one case per proof in :data:`ELIGIBILITY_PROOFS`.
"""

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Any

from due_work_harness.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    SELECTION_AUTHORING_OPERATIONS,
    assert_binding_reaches_production,
)
from due_work_harness.models import HarnessModel


class ExecutionGate[IdentityT, SnapshotT, ObservationT](HarnessModel):
    """One blocked, already time-due obligation, described so the proofs can drive it."""

    name: str

    #: The obligation's identity, as ``due_work`` and ``owed_work`` report it.
    identity: IdentityT

    #: The production selection of eligible work. The blocked obligation must not be in it.
    due_work: Callable[[], Iterable[IdentityT]]

    #: Everything production still owes, blocked or not. The obligation must stay in it.
    owed_work: Callable[[], Iterable[IdentityT]]

    #: An immutable observation covering the obligation's identity and revision,
    #: attempts, retry budget and not-before time. No persisted field names or
    #: states are prescribed.
    reserved_state: Callable[[], SnapshotT]

    #: The production entry points that could execute the blocked work (a task, a
    #: request handler, a scheduled runner), by name. Each is run while the work is blocked.
    routes: Mapping[str, Callable[[], object]]

    #: Lift the blocker through production's own transition, with the readiness
    #: notification lost: nothing but recovery may look at the work afterwards.
    make_eligible: Callable[[], None]

    #: One run of production's recovery: the sweep, the periodic inspection.
    recover: Callable[[], object]

    #: Move the clock the gate's not-before and inspection times are measured on.
    advance: Callable[[timedelta], None]

    #: How many times production has executed the obligation.
    executions: Callable[[], int]

    #: An independent count of durable mutations. It catches a write that
    #: restores the same state, which ``reserved_state`` cannot see.
    mutation_count: Callable[[], int]

    #: A comparable snapshot of the product state the obligation completes.
    observe: Callable[[], ObservationT]

    #: What :attr:`observe` reports once the obligation has completed.
    expected: ObservationT

    #: How many times the external provider was called.
    effect_calls: Callable[[], int]

    #: The step recovery is given between attempts while waiting for completion.
    recovery_interval: timedelta

    #: How long recovery may take to complete an eligible obligation, and how
    #: far the blocked observation extends when nothing is scheduled.
    recovery_timeout: timedelta

    #: When production inspects the blocked work again, measured from admission
    #: of this already time-due example. ``None`` means notification- or
    #: database-driven only: :attr:`recovery_timeout` then bounds the negative
    #: observation.
    recheck_after: timedelta | None = None

    def model_post_init(self, _context: Any) -> None:
        if not self.routes or any(not name.strip() for name in self.routes):
            raise ValueError("ExecutionGate needs named execution routes")
        if self.recovery_interval <= timedelta(0) or self.recovery_timeout < self.recovery_interval:
            raise ValueError("ExecutionGate needs a positive interval and a covering recovery timeout")
        if self.recheck_after is not None and self.recheck_after <= timedelta(0):
            raise ValueError("ExecutionGate recheck_after must be positive")


#: One binding shape shared by native lifecycles and work-table adapters: a factory
#: called fresh per generated case, returning the gate or a context manager yielding it.
type ExecutionGateBinding = Callable[[], ExecutionGate | AbstractContextManager[ExecutionGate]]


def assert_gate_bindings_are_production_bound(gate: ExecutionGate) -> None:
    """The routes and recovery invoke production, and the selection is production's, not a test's copy."""
    for name, callback in (*gate.routes.items(), ("recover", gate.recover)):
        assert_binding_reaches_production(
            adopter=gate.name,
            field=name,
            binding=callback,
            forbidden=INVOCATION_AUTHORING_OPERATIONS,
            production_shape="the production execution or recovery entry point",
        )
    assert_binding_reaches_production(
        adopter=gate.name,
        field="due_work",
        binding=gate.due_work,
        forbidden=SELECTION_AUTHORING_OPERATIONS,
        production_shape="the production eligible selection",
    )


def assert_blocked_gate_preserves_intent(gate: ExecutionGate) -> None:
    """
    Blocked work stays owed and unselected, and no route or recovery touches it.

    Every route and recovery is run; none may lose the obligation, write durable
    state, change the reserved intent, execute, call the provider or change the
    product state. The example must not already be complete, or "unchanged"
    would prove nothing.
    """
    assert gate.identity in gate.owed_work(), f"{gate.name}: blocked intent must remain owed"
    assert gate.identity not in gate.due_work(), f"{gate.name}: blocked intent was selected"
    state, outcome, calls, executions = gate.reserved_state(), gate.observe(), gate.effect_calls(), gate.executions()
    assert outcome != gate.expected, f"{gate.name}: positive control: example is already complete"
    mutations = gate.mutation_count()
    for name, run in (*gate.routes.items(), ("recovery", gate.recover)):
        run()
        assert gate.identity in gate.owed_work(), f"{gate.name}: {name}: blocked intent was lost"
        assert gate.mutation_count() == mutations, f"{gate.name}: {name}: blocked work mutated durable state"
        assert gate.reserved_state() == state, f"{gate.name}: {name}: blocked execution mutated reserved state"
        assert gate.executions() == executions, f"{gate.name}: {name}: blocked work executed"
        assert gate.effect_calls() == calls, f"{gate.name}: {name}: blocked work called its provider"
        assert gate.observe() == outcome, f"{gate.name}: {name}: blocked work changed product state"


def assert_eligible_gate_recovers_lost_notification(gate: ExecutionGate) -> None:
    """
    Once eligible, recovery alone completes the work, and readiness released it unchanged.

    The readiness notification is lost by construction, so only production's own
    recovery can find the work. Becoming eligible releases an existing
    obligation: it must not admit a new revision, reset the retry budget or
    bring the not-before time forward. The provider must actually be reached,
    or completion was faked.
    """
    assert_blocked_gate_preserves_intent(gate)
    calls = gate.effect_calls()
    reserved = gate.reserved_state()
    gate.make_eligible()
    assert gate.reserved_state() == reserved, f"{gate.name}: readiness rewrote reserved intent or retry state"
    assert gate.identity in gate.due_work(), f"{gate.name}: eligible intent is absent from production selection"
    elapsed = timedelta(0)
    while True:
        gate.recover()
        if gate.observe() == gate.expected:
            break
        assert elapsed < gate.recovery_timeout, (
            f"{gate.name}: eligible recovery did not reach the declared product outcome"
        )
        step = min(gate.recovery_interval, gate.recovery_timeout - elapsed)
        gate.advance(step)
        elapsed += step
    assert gate.effect_calls() > calls, f"{gate.name}: positive control: completion never reached the provider"


def assert_periodic_inspection_is_bounded(gate: ExecutionGate) -> None:
    """
    The fallback inspection happens on its declared boundary: not before, not never, not on every run.

    With ``recheck_after`` the clock is moved to one microsecond before the
    boundary (the work must still be blocked and untouched), then across it
    (the work must be selected, and recovery must execute it once, then leave
    it alone until time moves again). Without one, the whole recovery timeout
    passes and the work must still be blocked and untouched.
    """
    assert_blocked_gate_preserves_intent(gate)
    if gate.recheck_after is None:
        gate.advance(gate.recovery_timeout)
        assert_blocked_gate_preserves_intent(gate)
        return
    gate.advance(gate.recheck_after - timedelta(microseconds=1))
    assert_blocked_gate_preserves_intent(gate)
    gate.advance(timedelta(microseconds=1))
    assert gate.identity in gate.due_work(), f"{gate.name}: periodic inspection never becomes due"
    before = gate.executions()
    gate.recover()
    assert gate.executions() > before, f"{gate.name}: periodic inspection never executed"
    after = gate.executions()
    gate.recover()
    assert gate.executions() == after, f"{gate.name}: periodic inspection hot-loops without advancing time"


ELIGIBILITY_PROOFS: tuple[Callable[[ExecutionGate], None], ...] = (
    assert_gate_bindings_are_production_bound,
    assert_blocked_gate_preserves_intent,
    assert_eligible_gate_recovers_lost_notification,
    assert_periodic_inspection_is_bounded,
)
