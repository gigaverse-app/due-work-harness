"""
Mechanical proofs that two claimed profiles describe the same work.

The failure this hunts was found live: a domain whose derivation (profile F's
``outstanding``) was one predicate and whose recovery sweep (profile A's
``due_work``) was a deliberately narrower one. Both profiles passed
independently — F re-derived the moved obligation, A recovered the fresh one —
while the rows in the difference were recoverable by nobody but a manually-run
backfill. Two green profiles, one permanently stranded class of work.
"""

from collections.abc import Callable
from typing import Any, Protocol

from due_work_harness.profiles.fact_derived_obligations import StateDerived


class RecoverySurface(Protocol):
    """
    The slice of a profile A adapter this composition proof reads.

    A :class:`~.profiles.automatic_recovery.DueWorkSweep` satisfies it — the contract
    layer passes one — and the harness's own self-tests satisfy it with a
    minimal in-memory surface, which is why the proof is typed to the slice
    rather than to the full sweep model.
    """

    @property
    def name(self) -> str: ...

    @property
    def due_work(self) -> Callable[[], Any]: ...

    @property
    def run_tick(self) -> Callable[[], Any]: ...

    @property
    def dispatched_ids(self) -> Callable[[], Any] | None: ...

    @property
    def identity_of(self) -> Callable[[Any], Any]: ...


def assert_automatic_recovery_consumes_derived_obligations(
    sweep: RecoverySurface,
    derived: StateDerived,
) -> None:
    """
    Profiles A and F agree on identity, eligibility, dispatch, and settlement.

    Two scenarios, because they strand differently. A *fresh* implied
    obligation is the ordinary case every sweep serves. A *settled-then-moved*
    obligation is the gap-filling blind spot: once settled, a sweep keyed on
    "never reached a terminal state" no longer sees the row, so a desired-state
    move afterwards re-opens the obligation for the derivation while recovery
    never selects it. Immutable lifecycles that replace the row instead of
    editing it declare the rotation with ``identity_after_move``.
    """
    assert derived.make_recovery_eligible is not None, (
        f"{derived.name}: profiles A and F are both claimed, but the derivation "
        "binding has no make_recovery_eligible callback. The composition proof "
        "must be able to age a real derived obligation into the recovery window"
    )
    assert sweep.dispatched_ids is not None, (
        f"{sweep.name}: profiles A and F are both claimed, but the sweep has no "
        "dispatched_ids observation. The contract cannot prove that recovery "
        "consumed the obligation produced by derivation"
    )

    identity = derived.make_implied_obligation()
    derived.derive()
    assert identity in derived.outstanding(), (
        f"{derived.name}: derivation did not expose its implied obligation before recovery"
    )
    derived.make_recovery_eligible(identity)
    selected = {sweep.identity_of(row) for row in sweep.due_work()}
    assert identity in selected, (
        f"{sweep.name}: automatic recovery does not select the obligation profile F derived ({identity!r})"
    )
    sweep.run_tick()
    assert identity in set(sweep.dispatched_ids()), (
        f"{sweep.name}: automatic recovery selected but did not dispatch profile F's obligation ({identity!r})"
    )
    derived.settle(identity)
    assert identity not in derived.outstanding(), (
        f"{derived.name}: the obligation remains outstanding after the production settlement path"
    )

    moved_from = derived.make_implied_obligation()
    derived.derive()
    derived.settle(moved_from)
    assert moved_from not in derived.outstanding(), (
        f"{derived.name}: settle() left the obligation outstanding, so this "
        f"proof cannot exercise the settled-then-moved scenario"
    )
    wanted = derived.move_desired_state(moved_from)
    moved_identity = derived.identity_after_move(moved_from)
    derived.derive()
    assert moved_identity in derived.outstanding(), (
        f"{derived.name}: a production desired-state move after settlement did not re-expose a recoverable obligation"
    )
    assert derived.desired_of(moved_identity) == wanted
    derived.make_recovery_eligible(moved_identity)
    moved_selected = {sweep.identity_of(row) for row in sweep.due_work()}
    assert moved_identity in moved_selected, (
        f"{sweep.name}: profile F re-derives the settled-then-moved obligation "
        f"({moved_identity!r}) as outstanding, but automatic recovery never "
        f"selects it. Both profiles pass on their own while recovery does not "
        f"consume everything derivation calls outstanding — this class of work "
        f"is stranded unless something else demonstrably owns it, and that "
        f"split must be a declared legacy gap on this proof, not a silent "
        f"property of two predicates"
    )
    sweep.run_tick()
    assert moved_identity in set(sweep.dispatched_ids()), (
        f"{sweep.name}: automatic recovery did not dispatch the settled-then-moved "
        f"profile-F obligation ({moved_identity!r})"
    )
