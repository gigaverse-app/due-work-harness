"""
Profile E of the due-work harness: eventual convergence (convergent, write-once results).

Profiles A–C cover work that is *outstanding*. This one covers what happens when
a result finally lands, and it matters most for what is usually the majority
class of effects: observable ones, where recovery works by re-deriving desired
state and repairing the difference. Every one of those replays, and a replay that can overwrite a newer
result is worse than no replay at all.

Two failure shapes, so two differently-shaped proofs:

**A superseded worker writing back.** A worker snapshots desired state, does slow
work, and returns to find the world moved on. If it writes anyway, it publishes a
result derived from stale input. An image-rendition worker, for example, guards
against exactly this by comparing the source key it was given against the row's
current one before doing anything.

**Later evidence downgrading settled state.** Reconciliation, retries and late
provider callbacks all re-apply outcomes, often out of order. If applying
evidence can move a final entry backwards, a slow reconciler silently undoes a
settled result. A delivery log whose entries are write-once once final — an
email campaign's per-recipient entries, say — exists for this reason.

The two are separate because they live at different layers — the first is about a
*worker* holding a stale snapshot, the second about a *state function* receiving
stale evidence — and an adopter commonly has one without the other. Each proof is
independently callable so an adopter declares only what it implements.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from due_work_harness.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    assert_binding_reaches_production,
    assert_test_binding_delegates_to_production,
    assert_test_binding_forwards,
)


@dataclass(frozen=True)
class SupersededSnapshot:
    """
    The worker half of profile E, described so the proofs can drive it.

    Separate from :class:`ConvergentWrite` because the two failure shapes live
    at different layers — see the module docstring — and an adopter commonly
    has one without the other.

    The shape models the real race rather than a fabricated stale input. An
    earlier form took one ``run_with_superseded_snapshot`` callable, and its
    first adopter satisfied it by calling the worker with a made-up source key
    the production dispatch path never sends — which proves the worker has a
    defensive branch, not that the domain's actual dispatch-then-move sequence
    is safe. Splitting the binding into *capture the production dispatch*, then
    *move the state through the production transition*, then *run what was
    captured* means the staleness is produced the way production produces it,
    and gives the positive control (:func:`assert_current_snapshot_writes`) the
    same captured invocation to run un-superseded.
    """

    name: str

    #: A comparable summary of the state the worker would write. Should cover
    #: readiness and failure state, not just one field, so a partial artifact
    #: is also visible.
    observe: Callable[[], Any]

    #: Capture the exact input production would hand to the worker now —
    #: usually by recording the task's dispatch arguments while driving the
    #: production dispatch path, the same recorder shape profile A's
    #: ``dispatched_ids`` uses.
    take_snapshot: Callable[[], Any]

    #: Move product state through its real production transition — a re-upload,
    #: a newer revision, a cancellation. Never a direct column write: a
    #: fabricated mismatch tests a branch production cannot reach.
    supersede: Callable[[], Any]

    #: Run the real worker with the previously captured input.
    run_with_snapshot: Callable[[Any], Any]


def assert_snapshot_transitions_are_production_bound(snapshot: SupersededSnapshot) -> None:
    """
    The adapter may observe a stale input; it may not manufacture one.

    Authorship AND delegation, both halves of the shared rule, on the two
    fields that carry semantics: the supersession and the worker invocation
    must neither re-implement production semantics in test code nor float free
    of production entirely (a plain-Python worker passing these proofs
    demonstrates only itself). ``take_snapshot`` is deliberately unchecked —
    it is capture/observe code (reading the row's current input, often off a
    plain model instance), and a *fabricated* input is self-defeating against
    the pairing: the positive control demands the captured input write while
    current, and the superseded proof demands the same input not write after
    the production supersession.
    """
    for field, binding, shape in (
        ("supersede", snapshot.supersede, "the production supersession transition"),
        ("run_with_snapshot", snapshot.run_with_snapshot, "the production worker invocation"),
    ):
        assert_binding_reaches_production(
            adopter=snapshot.name,
            field=field,
            binding=binding,
            forbidden=INVOCATION_AUTHORING_OPERATIONS,
            production_shape=shape,
        )


def assert_current_snapshot_writes(snapshot: SupersededSnapshot) -> None:
    """
    POSITIVE CONTROL: the captured input, un-superseded, does write.

    Without this, the superseded proof below is satisfiable by a binding that
    never reaches the write path at all — a worker invoked with input that
    makes it return early, or a capture that runs nothing. Observing no write
    is only evidence of a guard once the same input demonstrably writes while
    still current. The same pairing discipline as profile B's 3a/6a and
    profile F's invariants 1 and 4.
    """
    worker_input = snapshot.take_snapshot()
    before = snapshot.observe()
    snapshot.run_with_snapshot(worker_input)
    after = snapshot.observe()
    assert after != before, (
        f"{snapshot.name}: the worker ran with its captured input still "
        f"current and nothing observable changed ({before!r}). Either "
        f"run_with_snapshot never reaches the real worker or the observation "
        f"misses what it writes — in both cases the superseded-snapshot proof "
        f"is measuring a run that writes nothing, which passes vacuously"
    )


def assert_superseded_snapshot_does_not_write(snapshot: SupersededSnapshot) -> None:
    """
    A worker whose input is no longer current must not publish a result.

    The proof captures a real dispatch, moves the product state past it through
    the production transition, runs the captured invocation, and asserts
    nothing observable changed.
    """
    worker_input = snapshot.take_snapshot()
    snapshot.supersede()
    before = snapshot.observe()
    snapshot.run_with_snapshot(worker_input)
    after = snapshot.observe()
    assert after == before, (
        f"{snapshot.name}: a worker holding a superseded snapshot wrote back "
        f"({before!r} -> {after!r}). Its result was derived from input the row "
        f"has already moved past, so it publishes a stale effect and can "
        f"overwrite the newer one"
    )


SNAPSHOT_PROOFS: tuple[Callable[[SupersededSnapshot], None], ...] = (
    assert_snapshot_transitions_are_production_bound,
    assert_current_snapshot_writes,
    assert_superseded_snapshot_does_not_write,
)


def assert_superseded_snapshot_contract(snapshot: SupersededSnapshot) -> None:
    """Run every proof of the worker half against one adopter."""
    for proof in SNAPSHOT_PROOFS:
        proof(snapshot)


@dataclass(frozen=True)
class ConvergentWrite:
    """
    A domain's result-application function, described for the state proofs.

    Deliberately expressed over opaque state values rather than rows: a
    result-application state machine is often a pure function, and a contract
    that demanded a database would exclude it.
    """

    name: str

    #: A state in which the target is already final.
    settled_state: Callable[[], Any]

    #: A state in which the target is not yet final.
    unsettled_state: Callable[[], Any]

    #: Apply one piece of definite evidence to a state, returning the result.
    #: The same evidence every time, so re-application is observable.
    apply_evidence: Callable[[Any], Any]

    #: Whether a state counts as final for the target.
    is_settled: Callable[[Any], bool]


def assert_evidence_application_is_production_bound(convergence: ConvergentWrite) -> None:
    """The contract invokes the real evidence transition, never a test copy."""
    assert_test_binding_forwards(
        adopter=convergence.name,
        field="apply_evidence",
        binding=convergence.apply_evidence,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the production evidence-application transition",
    )


def assert_convergence_bindings_are_production_bound(convergence: ConvergentWrite) -> None:
    """
    INVARIANT 0, delegation half: evidence application REACHES runtime code.

    The authorship half (:func:`assert_evidence_application_is_production_bound`)
    runs with the behavioral tuple and rejects ORM re-implementations; it
    cannot reject a pure-Python state function implemented in the test module,
    which proves that function convergent and says nothing about the one
    production runs. This delegation check runs at the contract layer and in
    the composite, where the raw tuple's mutation catalogs are not in play.
    The state builders and ``is_settled`` are arrange/observe and stay
    unchecked, exactly as the shared boundary rule draws it.
    """
    assert_test_binding_delegates_to_production(
        adopter=convergence.name,
        field="apply_evidence",
        binding=convergence.apply_evidence,
        production_shape="the production evidence-application transition",
    )


def _unsettled(convergence: ConvergentWrite) -> Any:
    """
    The unsettled fixture, checked to actually be unsettled.

    POSITIVE CONTROL for the two proofs that consume it. ``settled_state`` has
    carried one since it was written — a settled fixture that is not settled
    makes the no-downgrade proof vacuous — and its dual was never added. It
    matters more, because one mistake in this one arrange binding voids TWO
    proofs at once: applying evidence to already-settled state is a no-op by
    the first invariant, so ``assert_reapplication_is_a_no_op`` passes for the
    wrong reason, and ``assert_unsettled_state_converges`` asserts a state is
    settled that arrived settled.
    """
    unsettled = convergence.unsettled_state()
    assert not convergence.is_settled(unsettled), (
        f"{convergence.name}: unsettled_state() produced a state is_settled() "
        f"already accepts, so convergence and re-application are both proven "
        f"against work that needed no convergence. Build the pre-evidence state"
    )
    return unsettled


def assert_settled_state_is_not_downgraded(convergence: ConvergentWrite) -> None:
    """Later evidence cannot move a final result backwards."""
    settled = convergence.settled_state()
    assert convergence.is_settled(settled), (
        f"{convergence.name}: settled_state() did not produce a settled state, so this proof would pass vacuously"
    )
    result = convergence.apply_evidence(settled)
    assert result == settled, (
        f"{convergence.name}: applying evidence to an already-settled state "
        f"changed it ({settled!r} -> {result!r}). A late or duplicated "
        f"reconciler would silently undo a result that was already final"
    )


def assert_reapplication_is_a_no_op(convergence: ConvergentWrite) -> None:
    """
    Applying the same evidence twice equals applying it once.

    Every recovery path in this design is at-least-once, so evidence *will* be
    applied more than once. Idempotence here is what makes that harmless.
    """
    unsettled = _unsettled(convergence)
    once = convergence.apply_evidence(unsettled)
    twice = convergence.apply_evidence(once)
    assert twice == once, (
        f"{convergence.name}: re-applying identical evidence changed the result "
        f"({once!r} -> {twice!r}). At-least-once delivery makes repeated "
        f"application certain, so it must be a no-op"
    )


def assert_unsettled_state_converges(convergence: ConvergentWrite) -> None:
    """
    Evidence actually settles work that was not settled.

    The complement of the two proofs above: an implementation that ignored all
    evidence would satisfy both of them while never converging at all.
    """
    result = convergence.apply_evidence(_unsettled(convergence))
    assert convergence.is_settled(result), (
        f"{convergence.name}: applying definite evidence to unsettled state did "
        f"not settle it ({result!r}). Rejecting stale writes must not come at "
        f"the cost of rejecting current ones"
    )


#: The behavioral proofs; the binding guard is composed in by the contract
#: layer and the composite below, keeping mutation catalogs behavioral.
CONVERGENT_WRITE_PROOFS: tuple[Callable[[ConvergentWrite], None], ...] = (
    assert_evidence_application_is_production_bound,
    assert_settled_state_is_not_downgraded,
    assert_reapplication_is_a_no_op,
    assert_unsettled_state_converges,
)


def assert_convergent_write_contract(convergence: ConvergentWrite) -> None:
    assert_convergence_bindings_are_production_bound(convergence)
    for proof in CONVERGENT_WRITE_PROOFS:
        proof(convergence)
