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
  loop** because inspecting the blocked work does not move its next inspection;
* the fallback inspection **runs the blocked work**: calls the provider,
  completes it, or drops it;
* the gate describes a selection and a recovery **other than the contract
  sweep's**, so every proof here judges code the sweep never runs.

:class:`ExecutionGate` describes one blocked, already time-due obligation with
its readiness notification lost. The bindings forward selection and execution
to production and expose observations; these proofs own the verdict. They know
no table, queue, lease or clock implementation, so a native lifecycle and a
work-table adapter bind the same contract. The clock is the adopter's
``advance``: the proofs move it across both sides of the declared boundary and
never sleep.

Declared on :class:`~due_work_harness.contract.DueWorkContract` as
``eligibility=`` (one gate, or named gates for named product blockers), which
generates one case per proof in :data:`ELIGIBILITY_PROOFS`, plus
:func:`assert_gate_is_recovered_by_the_contract_sweep` against the contract's
own sweep.
"""

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Any, Generic, TypeAlias, TypeVar

from due_work_harness.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    SELECTION_AUTHORING_OPERATIONS,
    TRANSITION_AUTHORING_OPERATIONS,
    assert_binding_reaches_production,
)
from due_work_harness.models import DueWorkContractDesignError, HarnessModel
from due_work_harness.profiles.automatic_recovery import DueWorkSweep

IdentityT = TypeVar("IdentityT")
SnapshotT = TypeVar("SnapshotT")
ObservationT = TypeVar("ObservationT")


class ExecutionGate(HarnessModel, Generic[IdentityT, SnapshotT, ObservationT]):
    """One blocked, already time-due obligation, described so the proofs can drive it."""

    name: str

    #: The obligation's identity, as ``due_work`` and ``owed_work`` report it,
    #: and as the contract sweep's ``identity_of`` reports its row.
    identity: IdentityT

    #: The production selection of eligible work, the one the contract's sweep
    #: runs. The blocked obligation must not be in it.
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

    #: How many times production has executed the obligation. Unless
    #: :attr:`inspections` is declared, a periodic inspection of blocked work
    #: counts here too.
    executions: Callable[[], int]

    #: How many times production has inspected the blocked work, for a design whose
    #: periodic inspection re-checks the blocker without executing anything. When
    #: declared, an inspection must move this and must not move :attr:`executions`.
    inspections: Callable[[], int] | None = None

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

    #: The smallest step :attr:`advance` really moves the clock by. The boundary is
    #: probed this far before ``recheck_after``: a clock counting whole seconds
    #: never moves by a microsecond, so it declares one second. It is also how early
    #: an inspection may fire unseen, so it may be at most one second and at most a
    #: tenth of ``recheck_after``.
    clock_resolution: timedelta = timedelta(microseconds=1)

    def model_post_init(self, _context: Any) -> None:
        if not self.routes or any(not name.strip() for name in self.routes):
            raise DueWorkContractDesignError("ExecutionGate needs named execution routes")
        if self.recovery_interval <= timedelta(0) or self.recovery_timeout < self.recovery_interval:
            raise DueWorkContractDesignError("ExecutionGate needs a positive interval and a covering recovery timeout")
        if self.recheck_after is not None and self.recheck_after <= timedelta(0):
            raise DueWorkContractDesignError("ExecutionGate recheck_after must be positive")
        if (
            self.clock_resolution <= timedelta(0)
            or self.clock_resolution > MAX_CLOCK_RESOLUTION
            or (self.recheck_after is not None and self.clock_resolution * 10 > self.recheck_after)
        ):
            raise DueWorkContractDesignError(
                f"ExecutionGate clock_resolution must be positive, at most {MAX_CLOCK_RESOLUTION} and at most a "
                f"tenth of recheck_after: it is how early an inspection may fire unseen"
            )


#: The coarsest clock a gate may declare; see :attr:`ExecutionGate.clock_resolution`.
MAX_CLOCK_RESOLUTION = timedelta(seconds=1)


#: One binding shape shared by native lifecycles and work-table adapters: a factory
#: called fresh per generated case, returning the gate or a context manager yielding it.
ExecutionGateBinding: TypeAlias = Callable[[], ExecutionGate | AbstractContextManager[ExecutionGate]]


def assert_gate_bindings_are_production_bound(gate: ExecutionGate) -> None:
    """
    Every binding that decides the verdict reaches production, not a test's copy.

    The routes and recovery invoke production; the eligible and owed selections
    are production's (a test-written ``owed_work`` that always reports the
    obligation hides its loss); readiness is production's transition (a
    test-written one that flips a flag hides what the real one rewrites).
    """
    for name, callback in (*gate.routes.items(), ("recover", gate.recover)):
        assert_binding_reaches_production(
            adopter=gate.name,
            field=name,
            binding=callback,
            forbidden=INVOCATION_AUTHORING_OPERATIONS,
            production_shape="the production execution or recovery entry point",
        )
    for field, binding, shape in (
        ("due_work", gate.due_work, "the production eligible selection"),
        ("owed_work", gate.owed_work, "the production selection of everything still owed"),
    ):
        assert_binding_reaches_production(
            adopter=gate.name,
            field=field,
            binding=binding,
            forbidden=SELECTION_AUTHORING_OPERATIONS,
            production_shape=shape,
        )
    assert_binding_reaches_production(
        adopter=gate.name,
        field="make_eligible",
        binding=gate.make_eligible,
        forbidden=TRANSITION_AUTHORING_OPERATIONS,
        production_shape="the production transition that lifts the blocker",
    )


def _inspected(gate: ExecutionGate) -> int:
    """How many inspections have happened: the declared count, or executions when inspecting executes."""
    return gate.inspections() if gate.inspections is not None else gate.executions()


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
    mutations, inspections = gate.mutation_count(), _inspected(gate)
    for name, run in (*gate.routes.items(), ("recovery", gate.recover)):
        run()
        assert gate.identity in gate.owed_work(), f"{gate.name}: {name}: blocked intent was lost"
        assert gate.mutation_count() == mutations, f"{gate.name}: {name}: blocked work mutated durable state"
        assert gate.reserved_state() == state, f"{gate.name}: {name}: blocked execution mutated reserved state"
        assert gate.executions() == executions, f"{gate.name}: {name}: blocked work executed"
        assert _inspected(gate) == inspections, f"{gate.name}: {name}: blocked work was inspected before it was due"
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
    The fallback inspection happens on its declared boundary, again later, and inspects without running the work.

    With ``recheck_after``, two windows are probed. From admission, the clock is
    moved to :attr:`~ExecutionGate.clock_resolution` before the boundary (the work
    must be unselected, and no route or recovery may touch it), then across it
    (the work must be selected, and one recovery must inspect it). From that
    inspection, the clock is moved to just before the boundary again (nothing may
    run), then recovery runs in :attr:`~ExecutionGate.recovery_interval` steps
    until it inspects the work again, within the recovery timeout: later than
    ``recheck_after`` is allowed, as a continuation delay may push it, but never
    earlier and never not at all. At each inspection the provider must not be
    called, the product state must not change, the obligation must stay owed and,
    when inspections are observed apart, nothing may execute; a second recovery
    without time passing must not inspect again. Without ``recheck_after``, the
    whole recovery timeout passes and the work must still be blocked and untouched.
    """
    assert_blocked_gate_preserves_intent(gate)
    if gate.recheck_after is None:
        gate.advance(gate.recovery_timeout)
        assert_blocked_gate_preserves_intent(gate)
        return
    _not_before_the_boundary(gate, gate.recheck_after, "from admission")
    gate.advance(gate.clock_resolution)
    assert gate.identity in gate.due_work(), (
        f"{gate.name}: periodic inspection never becomes due once recheck_after has passed from admission: the "
        f"first inspection is armed later than recheck_after, or never, or the clock moves in steps coarser than "
        f"the declared clock_resolution ({gate.clock_resolution})"
    )
    _inspection_leaves_the_work_blocked(gate, _inspecting(gate, lambda: gate.recover()))
    _not_before_the_boundary(gate, gate.recheck_after, "since the last inspection")
    gate.advance(gate.clock_resolution)
    _inspection_leaves_the_work_blocked(gate, _inspecting(gate, lambda: _recover_until_inspected(gate)))


def _not_before_the_boundary(gate: ExecutionGate, recheck_after: timedelta, window: str) -> None:
    gate.advance(recheck_after - gate.clock_resolution)
    assert gate.identity not in gate.due_work(), (
        f"{gate.name}: blocked work is due before recheck_after has passed {window} ({recheck_after}, probed "
        f"{gate.clock_resolution} early): the periodic inspection runs too often"
    )
    assert_blocked_gate_preserves_intent(gate)


def _recover_until_inspected(gate: ExecutionGate) -> None:
    before, elapsed = _inspected(gate), timedelta(0)
    while True:
        gate.recover()
        if _inspected(gate) > before:
            return
        assert elapsed < gate.recovery_timeout, (
            f"{gate.name}: periodic inspection never inspected the blocked work again within "
            f"{gate.recovery_timeout} of recheck_after passing since the last inspection"
        )
        step = min(gate.recovery_interval, gate.recovery_timeout - elapsed)
        gate.advance(step)
        elapsed += step


def _inspecting(gate: ExecutionGate, inspect: Callable[[], object]) -> tuple[int, Any, int]:
    """Run ``inspect``, require it to inspect, and return what the blocked work looked like before it."""
    calls, outcome, executions, before = gate.effect_calls(), gate.observe(), gate.executions(), _inspected(gate)
    inspect()
    assert _inspected(gate) > before, (
        f"{gate.name}: periodic inspection never inspected the blocked work. An inspection that does not "
        f"execute declares `inspections`"
    )
    return calls, outcome, executions


def _inspection_leaves_the_work_blocked(gate: ExecutionGate, before: tuple[int, Any, int]) -> None:
    calls, outcome, executions = before
    assert gate.effect_calls() == calls, f"{gate.name}: the periodic inspection called the provider for blocked work"
    assert gate.observe() == outcome, f"{gate.name}: the periodic inspection changed the product state of blocked work"
    assert gate.identity in gate.owed_work(), f"{gate.name}: the periodic inspection dropped the blocked obligation"
    if gate.inspections is not None:
        assert gate.executions() == executions, f"{gate.name}: the periodic inspection executed the blocked work"
    after = _inspected(gate)
    gate.recover()
    assert _inspected(gate) == after, f"{gate.name}: periodic inspection hot-loops without advancing time"


ELIGIBILITY_PROOFS: tuple[Callable[[ExecutionGate], None], ...] = (
    assert_gate_bindings_are_production_bound,
    assert_blocked_gate_preserves_intent,
    assert_eligible_gate_recovers_lost_notification,
    assert_periodic_inspection_is_bounded,
)


def assert_gate_is_recovered_by_the_contract_sweep(gate: ExecutionGate, sweep: DueWorkSweep) -> None:
    """
    The gate describes the contract sweep's own selection and recovery, not another path to the same work.

    Every other eligibility proof trusts ``gate.due_work`` and ``gate.recover``.
    Bound to a directly invoked worker, or to a selection the sweep never runs,
    they would all pass while the sweep that profile A proves never releases the
    work. So the sweep's own selection (compared through its ``identity_of``) must
    leave the blocked work out and take it in once eligible, and ``gate.recover``
    must make the sweep's dispatch path send it: the number of times the gate's
    identity appears in the sweep's ``dispatched_ids``, the recorder of what its
    dispatch path sent, must rise while ``gate.recover`` runs. A readiness
    notification sent through the same path before recovery (and then lost)
    changes nothing. What is observed is the dispatch, not which code ran where,
    so a tick reached through a service, a module attribute, a task queue or
    another thread is recognised alike. Anything else that sends through the same
    recorded path during recovery (another sweep's tick, the path called without
    the tick) passes too; that residual is for review.
    """
    assert sweep.dispatched_ids is not None, (
        f"{gate.name}: the contract sweep {sweep.name!r} declares no dispatched_ids, so nothing shows whether "
        f"the gate's recovery runs it. Declare the recorder of what its tick dispatches"
    )

    def selected() -> set[Any]:
        return {sweep.identity_of(row) for row in sweep.due_work()}

    assert gate.identity not in selected(), f"{gate.name}: blocked work is in the contract sweep's selection"
    gate.make_eligible()
    assert gate.identity in selected(), (
        f"{gate.name}: eligible work is absent from the contract sweep's selection: the gate describes another "
        f"selection than the one {sweep.name!r} recovers"
    )
    before = list(sweep.dispatched_ids()).count(gate.identity)
    gate.recover()
    assert list(sweep.dispatched_ids()).count(gate.identity) > before, (
        f"{gate.name}: recover never dispatched {gate.identity!r} through the contract sweep {sweep.name!r}. Bind "
        f"the recovery that runs the sweep, not another path to the same worker"
    )
