"""
Reusable proof that blindly replaying one logical effect converges.

This is the ``REPLAY_SAFE_EXECUTION`` safety profile. Every adopter
explicitly claims, declines, marks not applicable, or records a known
gap for it in the flat domain contract, or a scoped :class:`~due_work_harness.contract.SafetyContract`; effects outside the
full due-work domain contract can declare the same safety contract directly. A
compatible claimant binds the production operation and an observation of its
externally meaningful result.

Three invariants, and the third is the one that keeps the other two honest:

1. **Positive control** — the first execution must visibly do something.
2. **Replay convergence** — executing the identical operation again must leave
   the same visible result.
3. **The replay actually ran** — the second execution must reach the external
   effect boundary too. Without it, an ``execute`` whose *selection* excludes
   the settled row is a no-op on replay: the observation is trivially equal,
   the claim passes, and what was really proven is that a settled row is not
   re-selected — which is profile A's invariant 2 wearing a replay-safety
   badge. That is the same "a proof must enter the state it names" rule that
   proofs about in-flight work follow, and it is why ``execution_count_for``
   observes the *substituted seam* — the external boundary the adopter
   replaced with a fake or recorder — rather than calls to ``execute``.

The observation belongs to the adopter because the meaningful result differs
by domain: persisted provider state, deterministic storage objects, or a local
projection. It should include every part of the effect whose duplication would
matter and exclude harmless telemetry such as duration samples.
"""

from collections.abc import Callable
from typing import Any

from due_work_harness.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    assert_binding_reaches_production,
    assert_test_binding_consumes_its_first_parameter,
)
from due_work_harness.evidence.observation import assert_observation
from due_work_harness.models import HarnessModel


class ReplaySafeEffect(HarnessModel):
    """One replayable production operation and its meaningful observation."""

    name: str

    #: Arrange one operation and return the opaque identity/input passed to
    #: ``execute``. Arrangement may use factories and test doubles.
    prepare: Callable[[], Any]

    #: Execute the real production operation once.
    execute: Callable[[Any], Any]

    #: Return a comparable projection of the externally meaningful result.
    observe: Callable[[Any], Any]

    #: How many times this identity reached the **external effect boundary** —
    #: the provider call, the storage write, the seam the adopter substituted.
    #: Not a count of ``execute`` calls: that is trivially one per invocation
    #: and proves nothing. The proofs require ``0 -> 1 -> 2`` around the
    #: replay, which is what separates "the effect is safe to repeat" from
    #: "the second call selected nothing to do".
    execution_count_for: Callable[[Any], int]


def assert_replay_transition_is_production_bound(effect: ReplaySafeEffect) -> None:
    """
    The operation under proof reaches production, on the identity it prepared.

    The delegation and authorship tripwires are the usual pair. The third check
    is specific to this profile's counterfeit: an ``execute`` that ignores the
    identity ``prepare`` returned is not replaying *that* operation, it is
    running whatever its own selection happens to find. A production-bound
    ``execute`` is exempt — it necessarily consumes its argument — so this only
    inspects adapter closures.
    """
    assert_test_binding_consumes_its_first_parameter(
        adopter=effect.name,
        field="execute",
        binding=effect.execute,
        parameter_shape="the identity `prepare` returned",
    )
    assert_binding_reaches_production(
        adopter=effect.name,
        field="execute",
        binding=effect.execute,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the production replayable operation",
    )


def assert_first_execution_has_visible_effect(effect: ReplaySafeEffect) -> None:
    """POSITIVE CONTROL: the bound operation changes its meaningful result."""
    operation = effect.prepare()
    before = effect.observe(operation)
    assert effect.execution_count_for(operation) == 0, (
        f"{effect.name}: the effect boundary was already reached before the "
        f"first execution, so nothing here can attribute an effect to it"
    )
    effect.execute(operation)
    reached = effect.execution_count_for(operation)
    assert reached == 1, (
        f"{effect.name}: the first execution reached the external effect "
        f"boundary {reached} time(s) rather than once. Bind "
        f"`execution_count_for` to the substituted seam — the provider call or "
        f"storage write — not to calls of `execute`"
    )
    after = effect.observe(operation)
    assert after != before, (
        f"{effect.name}: the first execution had no visible effect "
        f"({before!r} -> {after!r}). Replay convergence would therefore pass "
        f"vacuously for an operation that never ran or an observation that "
        f"cannot see its result"
    )


def assert_replay_converges(effect: ReplaySafeEffect) -> None:
    """Repeating the identical logical operation leaves the same result."""
    operation = effect.prepare()
    effect.execute(operation)
    after_first = effect.observe(operation)
    effect.execute(operation)
    replayed = effect.execution_count_for(operation)
    assert replayed == 2, (
        f"{effect.name}: the replay reached the external effect boundary "
        f"{replayed} time(s) against 2 expected. A second execution that never "
        f"reaches the effect converges trivially and proves only that "
        f"something upstream — a selection, a status guard — declined to run "
        f"it again. That is a real property, but it is not replay safety, and "
        f"a broker redelivery arriving before that guard commits does not get "
        f"it. Either bind `execute` to the effect itself, or claim the "
        f"property that actually holds"
    )
    after_replay = effect.observe(operation)
    assert_observation(
        after_replay,
        after_first,
        because=f"{effect.name}: replaying the identical operation changed its visible result; blind retry can duplicate or drift the external effect",
    )


REPLAY_SAFETY_PROOFS: tuple[Callable[[ReplaySafeEffect], None], ...] = (
    assert_replay_transition_is_production_bound,
    assert_first_execution_has_visible_effect,
    assert_replay_converges,
)


def assert_replay_safety_contract(effect: ReplaySafeEffect) -> None:
    """Run the production-binding guard and both replay invariants."""
    for proof in REPLAY_SAFETY_PROOFS:
        proof(effect)
