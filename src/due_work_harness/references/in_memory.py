"""
The harness's own conforming in-memory references — root-owned on purpose.

The delegation tripwire (:func:`due_work_harness.binding.assert_test_binding_delegates_to_production`)
rejects a semantic binding written in a test module that reaches no production
code, because an in-memory implementation passing the behavioral proofs proves
the harness works and nothing else. The harness's own self-tests need exactly
such implementations — they ARE testing the harness — so the conforming
references live here, in the package the tripwire treats as root-owned, with
one owner. The self-test modules keep their deliberately *broken* variants
locally, as subclasses of these, because mutants are exercised against
individual behavioral proofs where no binding guard runs.

That placement is also the fence. An adopter contract has no business binding
these: a domain suite that imports ``due_work_harness.references`` is measuring the
reference instead of the domain, and the import line says so to any reviewer.
The tripwire cannot distinguish that from the self-tests mechanically; the
loud, greppable import is the defense.

Also here: the stand-in ``assert_*`` proofs the contract self-tests delegate
to. The bespoke-assertion check resolves referenced callables and requires a
real harness-defined ``assert_*``; the self-tests exercise that machinery
through these stand-ins rather than by minting fake assert-named helpers in
test modules — which is precisely the counterfeit the resolution exists to
refuse.
"""

import itertools
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from due_work_harness.models import MutableHarnessModel
from due_work_harness.profiles.durable_retention import Retention
from due_work_harness.profiles.eventual_convergence import (
    ConvergentWrite,
    SupersededSnapshot,
)
from due_work_harness.profiles.fact_derived_obligations import StateDerived
from due_work_harness.safety.bounded_retry import BoundedRetry
from due_work_harness.safety.replay_safe_execution import ReplaySafeEffect

# --- Stand-in shared proofs ----------------------------------------------------


def assert_self_test_probe_fires(recorder: list[bool]) -> None:
    """A stand-in shared proof: records that it ran, asserts nothing."""
    recorder.append(True)


def assert_the_reference_capability_exists() -> None:
    """A stand-in shared proof that fails while the reference gap exists."""
    raise AssertionError("self-test: the deliberately absent capability is still absent")


# --- Standalone: replay safety -------------------------------------------------


class InMemoryReplayEffect:
    """A conforming operation whose stable identity is overwritten in place."""

    def __init__(self) -> None:
        self.outputs: dict[int, str] = {}
        self._ids = itertools.count(1)
        self.executions = 0
        self.effect_calls: dict[int, int] = {}

    def prepare(self) -> int:
        return next(self._ids)

    def execute(self, operation_id: int) -> None:
        self.executions += 1
        self.reach_effect(operation_id)
        self.outputs[operation_id] = "result"

    def reach_effect(self, operation_id: int) -> None:
        """
        Record that this identity crossed the effect boundary.

        Mutants that still perform the effect call this; mutants that model a
        skipped replay deliberately do not, which is the distinction
        :func:`~.safety.replay_safe_execution.assert_replay_converges` now requires.
        """
        self.effect_calls[operation_id] = self.effect_calls.get(operation_id, 0) + 1

    def observe(self, operation_id: int) -> str | None:
        return self.outputs.get(operation_id)

    def effect_count_for(self, operation_id: int) -> int:
        """Times this identity reached the (in-memory) effect boundary."""
        return self.effect_calls.get(operation_id, 0)


def reference_replay_safety_binding(
    effect: InMemoryReplayEffect | None = None,
    *,
    execute: Callable[[int], None] | None = None,
    execution_count_for: Callable[[int], int] | None = None,
) -> ReplaySafeEffect:
    """A replay-safe binding over the conforming reference or one mutant."""
    effect = effect if effect is not None else InMemoryReplayEffect()
    return ReplaySafeEffect(
        name="in-memory replay-safe effect",
        prepare=effect.prepare,
        execute=execute if execute is not None else effect.execute,
        observe=effect.observe,
        execution_count_for=execution_count_for if execution_count_for is not None else effect.effect_count_for,
    )


# --- Standalone: bounded retry -------------------------------------------------


class RetryRow(MutableHarnessModel):
    state: str = "DUE"
    executions: int = 0


class InMemoryRetryLifecycle:
    """A conforming three-execution retry lifecycle."""

    max_executions = 3

    def __init__(self) -> None:
        self.rows: dict[int, RetryRow] = {}
        self._ids = itertools.count(1)

    def make_failing(self) -> int:
        operation_id = next(self._ids)
        self.rows[operation_id] = RetryRow()
        return operation_id

    def run_once(self, operation_id: int) -> None:
        row = self.rows[operation_id]
        if row.state != "DUE":
            return
        row.executions += 1
        row.state = "TERMINAL" if row.executions >= self.max_executions else "WAITING"

    def due_work(self) -> list[int]:
        return [operation_id for operation_id, row in self.rows.items() if row.state == "DUE"]

    def advance_to_due(self, operation_id: int) -> None:
        row = self.rows[operation_id]
        if row.state == "WAITING":
            row.state = "DUE"

    def is_terminal(self, operation_id: int) -> bool:
        return self.rows[operation_id].state == "TERMINAL"

    def failure_attempt_count(self, operation_id: int) -> int:
        return self.rows[operation_id].executions

    def observe(self, operation_id: int) -> tuple[str, int]:
        row = self.rows[operation_id]
        return (row.state, row.executions)


def reference_bounded_retry_binding(
    lifecycle: InMemoryRetryLifecycle | None = None,
    *,
    run_once: Callable[[int], None] | None = None,
    observe: Callable[[int], Any] | None = None,
) -> BoundedRetry:
    """A bounded-retry binding over the conforming reference or one mutant."""
    lifecycle = lifecycle if lifecycle is not None else InMemoryRetryLifecycle()
    return BoundedRetry(
        name="in-memory bounded retry",
        max_executions=lifecycle.max_executions,
        make_failing=lifecycle.make_failing,
        due_work=lifecycle.due_work,
        run_once=run_once if run_once is not None else lifecycle.run_once,
        advance_to_due=lifecycle.advance_to_due,
        is_terminal=lifecycle.is_terminal,
        failure_attempt_count=lifecycle.failure_attempt_count,
        observe=observe if observe is not None else lifecycle.observe,
    )


# --- Profile B: fenced ownership -------------------------------------------------

REFERENCE_LEASE_SECONDS = 60.0


class OwnedRow(MutableHarnessModel):
    state: str = "READY"
    token: UUID | None = None
    lease_expires_at: float | None = None


class InMemoryOwner:
    """
    A minimal conforming fenced-lease implementation over a dict.

    Claiming issues a fresh token and a lease; the reaper re-arms only expired
    claims; every owner-gated mutation checks the token first and refuses with
    ``False``. The self-tests' broken variants each override exactly one of
    those properties.
    """

    def __init__(self) -> None:
        self.rows: dict[int, OwnedRow] = {}
        self._ids = itertools.count(1)
        self.clock = 0.0

    def make_claimable(self) -> int:
        row_id = next(self._ids)
        self.rows[row_id] = OwnedRow()
        return row_id

    def _issue_token(self) -> UUID:
        return uuid4()

    def claim(self) -> tuple[int, UUID] | None:
        for row_id, row in sorted(self.rows.items()):
            if row.state == "READY":
                row.state = "CLAIMED"
                row.token = self._issue_token()
                row.lease_expires_at = self.clock + REFERENCE_LEASE_SECONDS
                return (row_id, row.token)
        return None

    def fenced_write(self, row_id: int, token: UUID) -> bool:
        row = self.rows[row_id]
        if token != row.token:
            return False
        row.state = "RETRYABLE"
        row.lease_expires_at = None
        return True

    def renew_lease(self, row_id: int, token: UUID) -> bool:
        row = self.rows[row_id]
        if token != row.token:
            return False
        row.lease_expires_at = self.clock + REFERENCE_LEASE_SECONDS
        return True

    def expire_lease(self, row_id: int) -> None:
        self.rows[row_id].lease_expires_at = self.clock - 1.0

    def reclaim_stalled(self, row_id: int) -> str | None:
        row = self.rows[row_id]
        if row.state == "CLAIMED" and row.lease_expires_at is not None and row.lease_expires_at < self.clock:
            row.state = "READY"
            return "REARMED"
        return None

    def release_unchanged(self, row_id: int, token: UUID) -> bool:
        row = self.rows[row_id]
        if token != row.token:
            return False
        row.state = "READY"
        row.lease_expires_at = None
        return True

    def observe(self, row_id: int) -> tuple[str, float | None]:
        row = self.rows[row_id]
        return (row.state, row.lease_expires_at)


# --- Profile C: ambiguity-aware execution ----------------------------------------

REFERENCE_TERMINAL_STATES = ("SENT",)


class AmbiguousRow(MutableHarnessModel):
    state: str = "CLAIMED"
    token: UUID | None = None
    lease_expired: bool = False
    open_attempt: object | None = None


class InMemoryMachine:
    """
    A minimal conforming ambiguity-aware claim machine.

    The recovery disposition is exactly the profile's distinction: a dead claim
    holding an open attempt goes to ``UNKNOWN``; one with no attempt is re-armed
    ``READY``. Late evidence settles ``UNKNOWN`` rows to a terminal state and is
    refused-by-no-op afterwards.
    """

    def __init__(self) -> None:
        self.rows: dict[int, AmbiguousRow] = {}
        self._ids = itertools.count(1)

    def claim(self) -> tuple[int, UUID]:
        row_id = next(self._ids)
        token = uuid4()
        self.rows[row_id] = AmbiguousRow(token=token)
        return (row_id, token)

    def start_attempt(self, row_id: int, token: UUID) -> object:
        attempt = object()
        self.rows[row_id].open_attempt = attempt
        return attempt

    def open_attempt(self, row_id: int) -> object | None:
        return self.rows[row_id].open_attempt

    def expire_lease(self, row_id: int) -> None:
        self.rows[row_id].lease_expired = True

    def resolve_stalled(self, row_id: int) -> str:
        row = self.rows[row_id]
        if row.open_attempt is not None:
            row.state = "UNKNOWN"
            return "MARK_UNKNOWN"
        row.state = "READY"
        return "REARM_READY"

    def state_of(self, row_id: int) -> str:
        return self.rows[row_id].state

    def apply_late_evidence(self, row_id: int) -> str:
        row = self.rows[row_id]
        if row.state not in REFERENCE_TERMINAL_STATES:
            row.state = "SENT"
            row.open_attempt = None
        return row.state

    def due_work_ids(self) -> set[int]:
        return {row_id for row_id, row in self.rows.items() if row.state == "READY"}


# --- Profile E: convergent writes -------------------------------------------------

REFERENCE_SETTLED = ("SETTLED", ("evidence",))
REFERENCE_UNSETTLED = ("PENDING", None)


def reference_apply_evidence(state: tuple) -> tuple:
    """The conforming state function: settles once, then write-once."""
    phase, _payload = state
    if phase == "SETTLED":
        return state
    return REFERENCE_SETTLED


def reference_convergence_binding(
    apply_evidence: Callable[[tuple], tuple] = reference_apply_evidence,
) -> ConvergentWrite:
    """A profile E state-half binding; pass a broken apply to test a mutant."""
    return ConvergentWrite(
        name="in-memory state function",
        settled_state=lambda: REFERENCE_SETTLED,
        unsettled_state=lambda: REFERENCE_UNSETTLED,
        apply_evidence=apply_evidence,
        is_settled=lambda state: state[0] == "SETTLED",
    )


class GuardedWorker:
    """
    A worker that captures its input at dispatch and refuses a superseded one.

    The reference for profile E's worker half: ``take_snapshot`` reads the
    exact input production would hand the worker now, ``supersede`` moves the
    desired state on, and ``run`` publishes only when its captured input is
    still current. The self-tests' broken variants skip the guard or never
    write.
    """

    def __init__(self) -> None:
        self.current_key = "v1"
        self.published: str | None = None

    def take_snapshot(self) -> str:
        return self.current_key

    def supersede(self) -> str:
        self.current_key = f"{self.current_key}-moved"
        return self.current_key

    def run(self, snapshot_key: str) -> None:
        if snapshot_key != self.current_key:
            return
        self.published = f"result-from-{snapshot_key}"


def reference_snapshot_binding(worker: GuardedWorker | None = None) -> SupersededSnapshot:
    """A profile E worker-half binding over :class:`GuardedWorker` (or a mutant)."""
    worker = worker if worker is not None else GuardedWorker()
    return SupersededSnapshot(
        name="in-memory guarded worker",
        observe=lambda: worker.published,
        take_snapshot=worker.take_snapshot,
        supersede=worker.supersede,
        run_with_snapshot=worker.run,
    )


# --- Profile F: state-derived obligations ------------------------------------------


class Product(MutableHarnessModel):
    desired: str
    applied: str | None = None
    stopped: bool = False


class WorkRecord(MutableHarnessModel):
    desired: str
    revision: int = 0
    settled: bool = False


class MaterialisingDeriver:
    """
    A conforming reconciler: product state in, work records out.

    ``derive`` creates a record for any live product whose applied state does
    not match its desired state, and re-opens a settled record whose product
    moved on — carrying the *current* desired state. A pass over unchanged
    state writes nothing.
    """

    def __init__(self) -> None:
        self.products: dict[int, Product] = {}
        self.records: dict[int, WorkRecord] = {}
        self._ids = itertools.count(1)
        self._versions = itertools.count(1)

    def make_implied_obligation(self) -> int:
        product_id = next(self._ids)
        self.products[product_id] = Product(desired=f"v{next(self._versions)}")
        return product_id

    def make_stopped(self) -> int:
        product_id = self.make_implied_obligation()
        self.products[product_id].stopped = True
        return product_id

    def derive(self) -> None:
        for product_id, product in self.products.items():
            if product.stopped or product.applied == product.desired:
                continue
            record = self.records.get(product_id)
            if record is None:
                self.records[product_id] = WorkRecord(desired=product.desired)
            elif record.settled or record.desired != product.desired:
                record.desired = product.desired
                record.settled = False
                record.revision += 1

    def outstanding(self) -> list[int]:
        return sorted(product_id for product_id, record in self.records.items() if not record.settled)

    def record_of(self, product_id: int) -> tuple:
        record = self.records[product_id]
        return (record.desired, record.revision, record.settled)

    def settle(self, product_id: int) -> None:
        product = self.products[product_id]
        product.applied = product.desired
        record = self.records.get(product_id)
        if record is not None:  # tolerate a deriver that recorded nothing
            record.settled = True

    def move_desired_state(self, product_id: int) -> str:
        moved = f"v{next(self._versions)}"
        self.products[product_id].desired = moved
        return moved

    def desired_of(self, product_id: int) -> str:
        return self.records[product_id].desired


def materialising_derivation_binding(deriver: MaterialisingDeriver | None = None) -> StateDerived:
    """A profile F binding over :class:`MaterialisingDeriver` (or a mutant subclass)."""
    deriver = deriver if deriver is not None else MaterialisingDeriver()
    return StateDerived(
        name="in-memory materialising deriver",
        make_implied_obligation=deriver.make_implied_obligation,
        derive=deriver.derive,
        outstanding=deriver.outstanding,
        record_of=deriver.record_of,
        settle=deriver.settle,
        move_desired_state=deriver.move_desired_state,
        desired_of=deriver.desired_of,
        make_stopped=deriver.make_stopped,
        materialises_records=True,
    )


class EdgeTriggeredDeriver(MaterialisingDeriver):
    """
    The reference *edge-triggered* shape: obligations exist only when recorded.

    Root-owned (unlike the self-tests' other broken variants) because
    :class:`~.gap_probes.DisprovenCapability` self-tests need a binding whose
    fields pass the invariant-0 guards while the discriminating profile-F proof
    genuinely fails — the same shape an honest edge-triggered domain's decline
    evidence has.
    """

    def derive(self) -> None:
        return None


class SelectionIsTheDerivation:
    """The other conforming shape: no records, the selection is the derivation."""

    def __init__(self) -> None:
        self.products: dict[int, Product] = {}
        self._ids = itertools.count(1)
        self._versions = itertools.count(1)

    def make_implied_obligation(self) -> int:
        product_id = next(self._ids)
        self.products[product_id] = Product(desired=f"v{next(self._versions)}")
        return product_id

    def make_stopped(self) -> int:
        product_id = self.make_implied_obligation()
        self.products[product_id].stopped = True
        return product_id

    def outstanding(self) -> list[int]:
        return sorted(
            product_id
            for product_id, product in self.products.items()
            if not product.stopped and product.applied != product.desired
        )

    def record_of(self, product_id: int) -> tuple:
        product = self.products[product_id]
        return (product.desired, product.applied)

    def settle(self, product_id: int) -> None:
        product = self.products[product_id]
        product.applied = product.desired

    def move_desired_state(self, product_id: int) -> str:
        moved = f"v{next(self._versions)}"
        self.products[product_id].desired = moved
        return moved

    def desired_of(self, product_id: int) -> str:
        return self.products[product_id].desired


def reference_derivation_binding(impl: SelectionIsTheDerivation | None = None) -> StateDerived:
    """A profile F binding over :class:`SelectionIsTheDerivation`."""
    impl = impl if impl is not None else SelectionIsTheDerivation()
    return StateDerived(
        name="in-memory selection-is-derivation",
        make_implied_obligation=impl.make_implied_obligation,
        derive=lambda: None,
        outstanding=impl.outstanding,
        record_of=impl.record_of,
        settle=impl.settle,
        move_desired_state=impl.move_desired_state,
        desired_of=impl.desired_of,
        make_stopped=impl.make_stopped,
        materialises_records=False,
    )


# --- Profile D: retention ------------------------------------------------------------


class InMemoryRetention:
    """A conforming retention pass: prunes settled rows only, never owed ones."""

    def __init__(self) -> None:
        self.rows: dict[int, str] = {}
        self._ids = itertools.count(1)

    def make_non_terminal(self) -> int:
        row_id = next(self._ids)
        self.rows[row_id] = "OWED"
        return row_id

    def make_prunable(self) -> int:
        row_id = next(self._ids)
        self.rows[row_id] = "SETTLED"
        return row_id

    def run_retention(self) -> None:
        self.rows = {row_id: state for row_id, state in self.rows.items() if state != "SETTLED"}

    def still_exists(self, row_id: int) -> bool:
        return row_id in self.rows


def reference_retention_binding(impl: InMemoryRetention | None = None) -> Retention:
    """A profile D binding over :class:`InMemoryRetention` (or a mutant subclass)."""
    impl = impl if impl is not None else InMemoryRetention()
    return Retention(
        name="in-memory retention",
        make_non_terminal=impl.make_non_terminal,
        make_prunable=impl.make_prunable,
        run_retention=impl.run_retention,
        still_exists=impl.still_exists,
    )


__all__ = [
    "AmbiguousRow",
    "EdgeTriggeredDeriver",
    "GuardedWorker",
    "InMemoryMachine",
    "InMemoryOwner",
    "InMemoryRetention",
    "MaterialisingDeriver",
    "OwnedRow",
    "Product",
    "REFERENCE_LEASE_SECONDS",
    "REFERENCE_SETTLED",
    "REFERENCE_TERMINAL_STATES",
    "REFERENCE_UNSETTLED",
    "SelectionIsTheDerivation",
    "WorkRecord",
    "assert_self_test_probe_fires",
    "assert_the_reference_capability_exists",
    "materialising_derivation_binding",
    "reference_apply_evidence",
    "reference_convergence_binding",
    "reference_derivation_binding",
    "reference_retention_binding",
    "reference_snapshot_binding",
]
