"""
Profile C of the due-work harness: crash ambiguity (ambiguity-aware execution).

Discovery finds outstanding work; ownership decides who runs it. This profile
covers the part that no queue library or datastore provides: **what happens when
the provider's answer never arrives.**

The pivot is a single distinction. When an owner dies mid-effect, the recovery
path must be able to tell apart:

* a claim that died **before** calling the provider — nothing happened, so
  retrying is safe; and
* a claim that died **after** calling it — something may have happened, so
  retrying could duplicate a real-world effect.

Nothing in the row's state can distinguish those two *after the fact*. The only
way is to have committed a record of the attempt **before** the call. That is why
attempt-before-provider ordering is an invariant here rather than an
implementation detail, and it is the whole reason this profile exists.

One adapter invariant and six behavioral invariants:

0. **Ordinary due work is production-bound** — the adapter invokes the
   runtime's retry selection instead of reconstructing it in test code.

1. **Attempt recorded before the call** — an in-flight attempt is durably
   discoverable while the provider call is outstanding.
2. **A dead claim with an open attempt becomes ambiguous** — never a retry.
3. **A dead claim with no attempt is retryable** — the complement of 2. Together
   they are the distinction above; either one alone is satisfiable by a
   degenerate implementation that always picks the same branch.
4. **Ambiguity is not resolved by blind retry** — an ambiguous row is not
   re-dispatched as if it were ordinary due work.
5. **Late evidence resolves ambiguity** — an ambiguous row can reach a terminal
   state when the provider's outcome is learned afterwards, otherwise ambiguity
   is a permanent leak.
6. **Terminal is monotonic** — once terminal, later evidence cannot move it, so
   a slow reconciler cannot resurrect settled work.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from pytest_obligation.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    SELECTION_AUTHORING_OPERATIONS,
    assert_test_binding_delegates_to_production,
    assert_test_binding_forwards,
)
from pytest_obligation.models import HarnessModel


class AmbiguityAware(HarnessModel):
    """One domain's ambiguity handling, described so the proofs can drive it."""

    name: str

    #: Create one claimable row and claim it. Returns ``(row_id, token)``.
    claim: Callable[[], tuple[Any, UUID]]

    #: Record an attempt for a claimed row. Must be callable — and committed —
    #: BEFORE the provider call.
    start_attempt: Callable[[Any, UUID], Any]

    #: The row's open (unfinalized) attempt, or None.
    open_attempt: Callable[[Any], Any | None]

    #: Force the lease into the past, simulating an owner that died.
    expire_lease: Callable[[Any], None]

    #: The recovery path's disposition for a dead claim.
    resolve_stalled: Callable[[Any], Any]

    #: The disposition value meaning "outcome unknown".
    ambiguous_disposition: Any

    #: The disposition value meaning "safe to run again".
    retryable_disposition: Any

    #: The row's current state, as a plain string.
    state_of: Callable[[Any], str]

    #: The state name meaning "outcome unknown".
    ambiguous_state: str

    #: States from which no further transition is legal.
    terminal_states: tuple[str, ...]

    #: Apply late provider evidence to an ambiguous row. Returns the new state.
    apply_late_evidence: Callable[[Any], str]

    #: The due-work selection used for ordinary re-dispatch, as a collection of
    #: row identifiers. Invariant 4 asserts an ambiguous row is absent from it.
    due_work_ids: Callable[[], Any]


def assert_ambiguity_transitions_are_production_bound(ambiguity: AmbiguityAware) -> None:
    """
    INVARIANT 0a: ambiguity callbacks invoke production transitions.

    ``claim`` is deliberately absent from this authorship loop: its contract
    explicitly includes *arranging* the claimable row ("create one claimable
    row and claim it"), and arrange code legitimately reaches factories that
    ``create`` — checking it here would accuse the field's documented job.
    The claiming itself still cannot float free of production: the delegation
    half (:func:`assert_ambiguity_bindings_are_production_bound`) checks it.
    """
    for field, binding, shape in (
        ("start_attempt", ambiguity.start_attempt, "the production attempt-start transition"),
        ("resolve_stalled", ambiguity.resolve_stalled, "the production stalled-work resolution"),
        ("apply_late_evidence", ambiguity.apply_late_evidence, "the production evidence-application transition"),
    ):
        assert_test_binding_forwards(
            adopter=ambiguity.name,
            field=field,
            binding=binding,
            forbidden=INVOCATION_AUTHORING_OPERATIONS,
            production_shape=shape,
        )


def assert_ordinary_due_work_is_production_bound(ambiguity: AmbiguityAware) -> None:
    """INVARIANT 0: ordinary retry selection is runtime code, not a test copy."""
    assert_test_binding_forwards(
        adopter=ambiguity.name,
        field="due_work_ids",
        binding=ambiguity.due_work_ids,
        forbidden=SELECTION_AUTHORING_OPERATIONS,
        production_shape="the ordinary retry selection",
    )


def assert_ambiguity_bindings_are_production_bound(ambiguity: AmbiguityAware) -> None:
    """
    INVARIANT 0b, delegation half: the profile's transitions REACH runtime code.

    The authorship halves
    (:func:`assert_ambiguity_transitions_are_production_bound`,
    :func:`assert_ordinary_due_work_is_production_bound`) run with the
    behavioral tuple and reject ORM re-implementations. What they cannot reject
    is a test-side state machine over a dict, which passes every behavioral
    proof of this profile — attempt-before-provider, ambiguous-versus-
    retryable, terminal monotonicity — while the production reaper it stands in
    for does none of it. This delegation check runs at the contract layer and
    in the composite, where the raw tuple's mutation catalogs are not in play.
    ``claim`` is included: its contract covers arranging the claimable row,
    but the claiming itself — the path that issues the token every other
    transition is fenced by — must still be production's. ``open_attempt`` and
    ``state_of`` are observers and stay unchecked.
    """
    for field_name in ("claim", "start_attempt", "resolve_stalled", "apply_late_evidence"):
        binding = getattr(ambiguity, field_name)
        assert_test_binding_delegates_to_production(
            adopter=ambiguity.name,
            field=field_name,
            binding=binding,
            production_shape=f"the production {field_name.replace('_', ' ')} transition",
        )


def assert_attempt_is_recorded_before_the_provider_call(
    ambiguity: AmbiguityAware,
) -> None:
    """INVARIANT 1: an in-flight attempt is durably discoverable."""
    row_id, token = ambiguity.claim()
    assert ambiguity.open_attempt(row_id) is None, (
        f"{ambiguity.name}: a freshly claimed row already reports an open "
        f"attempt, so this proof cannot distinguish before from after"
    )
    ambiguity.start_attempt(row_id, token)
    assert ambiguity.open_attempt(row_id) is not None, (
        f"{ambiguity.name}: no open attempt is visible after start_attempt. "
        f"If the attempt is only recorded once the provider replies, a crash "
        f"during the call is indistinguishable from a crash before it"
    )


def assert_dead_claim_with_open_attempt_is_ambiguous(
    ambiguity: AmbiguityAware,
) -> None:
    """INVARIANT 2: a maybe-applied effect is never blindly retried."""
    row_id, token = ambiguity.claim()
    ambiguity.start_attempt(row_id, token)
    ambiguity.expire_lease(row_id)

    disposition = ambiguity.resolve_stalled(row_id)
    assert disposition == ambiguity.ambiguous_disposition, (
        f"{ambiguity.name}: a dead claim holding an OPEN attempt resolved to "
        f"{disposition!r}, not {ambiguity.ambiguous_disposition!r}. The provider "
        f"may already have applied the effect, so this path would duplicate it"
    )
    assert ambiguity.state_of(row_id) == ambiguity.ambiguous_state, (
        f"{ambiguity.name}: the row did not land in {ambiguity.ambiguous_state!r} after an ambiguous disposition"
    )


def assert_dead_claim_without_attempt_is_retryable(
    ambiguity: AmbiguityAware,
) -> None:
    """
    INVARIANT 3: work that provably never ran is not stranded as ambiguous.

    The complement of invariant 2. Without this, an implementation that marks
    *everything* ambiguous would pass invariant 2 while never retrying anything
    — safe but useless.
    """
    row_id, _ = ambiguity.claim()
    assert ambiguity.open_attempt(row_id) is None, f"{ambiguity.name}: expected no attempt before the provider call"
    ambiguity.expire_lease(row_id)

    disposition = ambiguity.resolve_stalled(row_id)
    assert disposition == ambiguity.retryable_disposition, (
        f"{ambiguity.name}: a dead claim that never recorded an attempt "
        f"resolved to {disposition!r}, not {ambiguity.retryable_disposition!r}. "
        f"The provider was never called, so this work is safe to retry and "
        f"treating it as ambiguous strands it behind manual reconciliation"
    )


def assert_ambiguity_is_not_resolved_by_blind_retry(
    ambiguity: AmbiguityAware,
) -> None:
    """INVARIANT 4: an ambiguous row is not ordinary due work."""
    row_id, token = ambiguity.claim()
    ambiguity.start_attempt(row_id, token)
    ambiguity.expire_lease(row_id)
    ambiguity.resolve_stalled(row_id)
    assert ambiguity.state_of(row_id) == ambiguity.ambiguous_state

    assert row_id not in ambiguity.due_work_ids(), (
        f"{ambiguity.name}: an ambiguous row appears in the ordinary due-work "
        f"selection, so the scheduled sweep would re-dispatch it and duplicate "
        f"a possibly-applied effect. Ambiguity must be resolved by evidence, "
        f"not by retry"
    )


def assert_late_evidence_resolves_ambiguity(ambiguity: AmbiguityAware) -> None:
    """INVARIANT 5: ambiguity is not a permanent leak."""
    row_id, token = ambiguity.claim()
    ambiguity.start_attempt(row_id, token)
    ambiguity.expire_lease(row_id)
    ambiguity.resolve_stalled(row_id)
    assert ambiguity.state_of(row_id) == ambiguity.ambiguous_state

    resulting = ambiguity.apply_late_evidence(row_id)
    assert resulting in ambiguity.terminal_states, (
        f"{ambiguity.name}: applying late provider evidence to an ambiguous row "
        f"produced {resulting!r}, which is not terminal "
        f"({ambiguity.terminal_states!r}). Ambiguous work must have a path to a "
        f"definite outcome or it accumulates forever"
    )


def assert_terminal_is_monotonic(ambiguity: AmbiguityAware) -> None:
    """INVARIANT 6: settled work cannot be resurrected."""
    row_id, token = ambiguity.claim()
    ambiguity.start_attempt(row_id, token)
    ambiguity.expire_lease(row_id)
    ambiguity.resolve_stalled(row_id)
    settled = ambiguity.apply_late_evidence(row_id)
    assert settled in ambiguity.terminal_states

    try:
        again = ambiguity.apply_late_evidence(row_id)
    except Exception:
        return  # refusing a second application is a legitimate design
    assert again == settled, (
        f"{ambiguity.name}: re-applying evidence moved a terminal row from "
        f"{settled!r} to {again!r}. A slow or duplicated reconciler must not be "
        f"able to reopen settled work"
    )


AMBIGUITY_PROOFS: tuple[Callable[[AmbiguityAware], None], ...] = (
    assert_ambiguity_transitions_are_production_bound,
    assert_ordinary_due_work_is_production_bound,
    assert_attempt_is_recorded_before_the_provider_call,
    assert_dead_claim_with_open_attempt_is_ambiguous,
    assert_dead_claim_without_attempt_is_retryable,
    assert_ambiguity_is_not_resolved_by_blind_retry,
    assert_late_evidence_resolves_ambiguity,
    assert_terminal_is_monotonic,
)


def assert_ambiguity_contract(ambiguity: AmbiguityAware) -> None:
    assert_ambiguity_bindings_are_production_bound(ambiguity)
    for proof in AMBIGUITY_PROOFS:
        proof(ambiguity)
