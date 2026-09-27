"""
Reusable proof that retryable failure has a finite, stable lifecycle.

This is the ``BOUNDED_RETRY`` safety profile. Every adopter explicitly
assesses it through :class:`~.contract.SafetyContract`; effects
outside the six lifecycle profiles can declare the same safety contract
directly. A claimant deliberately retries a classified transient failure and
declares a maximum number of executions, including the initial execution.

The proof does not prescribe a stored retry counter, backoff formula, or
terminal state name. It does require operation-specific evidence at the
injected failing dependency, because lifecycle state alone can advance without
the claimed operation ever running. It drives the real selector and runner,
lets the adopter advance only the waiting condition, and observes four shared
properties:

1. production selection finds the fresh operation and every pre-limit retry;
2. every runner invocation reaches the failing dependency exactly once;
3. the operation becomes terminal exactly at the declared execution bound and
   leaves ordinary due selection; and
4. invoking the runner once more cannot change terminal observable state or
   reach the failing dependency again.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from due_work_harness.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    SELECTION_AUTHORING_OPERATIONS,
    assert_binding_reaches_production,
)


def _identity(value: Any) -> Any:
    return value


@dataclass(frozen=True)
class BoundedRetry:
    """One production retry lifecycle under a persistent transient failure."""

    name: str

    #: Total failing executions allowed, including the initial execution.
    max_executions: int

    #: Arrange one due operation whose dependency will fail transiently.
    make_failing: Callable[[], Any]

    #: The ordinary production selection that finds retryable work. The
    #: harness, not the adopter, decides whether the arranged operation is in
    #: it; accepting an ``is_due`` verdict would let test code restate or fake
    #: the production predicate.
    due_work: Callable[[], Iterable[Any]]

    #: Run the real production executor or scheduled runner once.
    run_once: Callable[[Any], Any]

    #: Advance only the retry wait condition (for example, age scheduled_at).
    #: It may be a no-op for designs whose retry is immediately due.
    advance_to_due: Callable[[Any], Any]

    #: Whether the domain has durably exhausted/abandoned the operation.
    is_terminal: Callable[[Any], bool]

    #: Operation-specific count recorded at the injected failing dependency
    #: boundary. This is deliberately not just a lifecycle counter: it is the
    #: positive control proving each claimed retry reached the failure whose
    #: retry behavior is under test.
    failure_attempt_count: Callable[[Any], int]

    #: Comparable state broad enough to expose a terminal replay, including
    #: provider-call records when an extra call would itself be harmful.
    observe: Callable[[Any], Any]

    #: Convert both the arranged operation and each selected item to the same
    #: stable identity. The default covers selectors that return the operation
    #: identity directly; model-based adopters commonly bind ``lambda row:
    #: row.pk`` and arrange a real row.
    identity_of: Callable[[Any], Any] = _identity

    def __post_init__(self) -> None:
        if self.max_executions < 1:
            raise ValueError("BoundedRetry.max_executions must be at least one")


def assert_retry_runner_is_production_bound(retry: BoundedRetry) -> None:
    """The failing execution reaches production rather than a test state machine."""
    assert_binding_reaches_production(
        adopter=retry.name,
        field="run_once",
        binding=retry.run_once,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the production retry runner",
    )


def assert_retry_selection_is_production_bound(retry: BoundedRetry) -> None:
    """The due-work predicate forwards to production rather than test logic."""
    assert_binding_reaches_production(
        adopter=retry.name,
        field="due_work",
        binding=retry.due_work,
        forbidden=SELECTION_AUTHORING_OPERATIONS,
        production_shape="the production retry selection",
    )


def _is_due(retry: BoundedRetry, operation: Any) -> bool:
    identity = retry.identity_of(operation)
    return any(retry.identity_of(selected) == identity for selected in retry.due_work())


def _assert_one_failure_attempt(
    retry: BoundedRetry,
    operation: Any,
    *,
    before: int,
    execution: int,
) -> int:
    after = retry.failure_attempt_count(operation)
    new_attempts = after - before
    assert new_attempts == 1, (
        f"{retry.name}: execution {execution} ran the retry runner, but the "
        f"failing dependency recorded {new_attempts} new attempts "
        f"({before} -> {after}). Retry lifecycle state is not evidence that "
        f"the claimed failing operation actually ran exactly once"
    )
    return after


def _drive_to_terminal(retry: BoundedRetry) -> Any:
    operation = retry.make_failing()
    assert _is_due(retry, operation), (
        f"{retry.name}: make_failing() did not create due work, so the retry runner cannot be exercised"
    )
    assert not retry.is_terminal(operation), f"{retry.name}: make_failing() created already-terminal work"
    attempts = retry.failure_attempt_count(operation)
    assert attempts == 0, (
        f"{retry.name}: make_failing() created work with {attempts} recorded "
        f"failure attempt(s). The contract drives a fresh lifecycle from zero"
    )

    for execution in range(1, retry.max_executions + 1):
        retry.run_once(operation)
        attempts = _assert_one_failure_attempt(
            retry,
            operation,
            before=attempts,
            execution=execution,
        )
        if execution < retry.max_executions:
            assert not retry.is_terminal(operation), (
                f"{retry.name}: retryable failure became terminal after "
                f"{execution} execution(s), before the declared "
                f"{retry.max_executions}-execution bound"
            )
            retry.advance_to_due(operation)
            after_advance = retry.failure_attempt_count(operation)
            assert after_advance == attempts, (
                f"{retry.name}: advance_to_due changed the failure-attempt "
                f"count ({attempts} -> {after_advance}). Fault injection may "
                f"advance only the wait condition; it cannot manufacture an "
                f"execution the production runner never performed"
            )
            assert _is_due(retry, operation), (
                f"{retry.name}: retryable failure {execution} never became due again after its wait condition elapsed"
            )

    assert retry.is_terminal(operation), (
        f"{retry.name}: work is still non-terminal after the declared {retry.max_executions}-execution retry bound"
    )
    assert not _is_due(retry, operation), (
        f"{retry.name}: terminal retry-exhausted work remains in ordinary due selection and will execute forever"
    )
    return operation


def assert_retryable_failures_reach_terminal_at_the_bound(retry: BoundedRetry) -> None:
    """Transient failures remain recoverable, then exhaust at the exact bound."""
    _drive_to_terminal(retry)


def assert_terminal_retry_is_a_no_op(retry: BoundedRetry) -> None:
    """A stale duplicate runner cannot resurrect or re-execute exhausted work."""
    operation = _drive_to_terminal(retry)
    before = retry.observe(operation)
    attempts_before = retry.failure_attempt_count(operation)
    retry.run_once(operation)
    after = retry.observe(operation)
    assert after == before, (
        f"{retry.name}: terminal retry changed observable state "
        f"({before!r} -> {after!r}). A stale duplicate runner can re-execute "
        f"work after its retry budget is exhausted"
    )
    attempts_after = retry.failure_attempt_count(operation)
    assert attempts_after == attempts_before, (
        f"{retry.name}: terminal retry recorded another failure attempt "
        f"({attempts_before} -> {attempts_after}). A stale runner reached the "
        f"dependency after the retry budget was exhausted"
    )
    assert retry.is_terminal(operation) and not _is_due(retry, operation), (
        f"{retry.name}: invoking the runner resurrected terminal work"
    )


BOUNDED_RETRY_PROOFS: tuple[Callable[[BoundedRetry], None], ...] = (
    assert_retry_selection_is_production_bound,
    assert_retry_runner_is_production_bound,
    assert_retryable_failures_reach_terminal_at_the_bound,
    assert_terminal_retry_is_a_no_op,
)


def assert_bounded_retry_contract(retry: BoundedRetry) -> None:
    """Run the production-binding guard and bounded retry invariants."""
    for proof in BOUNDED_RETRY_PROOFS:
        proof(retry)
