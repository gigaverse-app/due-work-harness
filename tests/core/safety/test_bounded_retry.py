"""The generic bounded-retry proof, exercised against lifecycle mutations."""

from dataclasses import replace

import pytest

from due_work_harness.references.in_memory import (
    InMemoryRetryLifecycle,
    reference_bounded_retry_binding,
)
from due_work_harness.safety.bounded_retry import (
    assert_bounded_retry_contract,
    assert_retry_runner_is_production_bound,
    assert_retryable_failures_reach_terminal_at_the_bound,
    assert_terminal_retry_is_a_no_op,
)


def test_the_conforming_retry_lifecycle_passes_every_proof() -> None:
    assert_bounded_retry_contract(reference_bounded_retry_binding())


def test_a_retry_lifecycle_that_never_exhausts_fails_the_bound() -> None:
    class _NeverExhausts(InMemoryRetryLifecycle):
        def run_once(self, operation_id: int) -> None:
            row = self.rows[operation_id]
            row.executions += 1
            row.state = "WAITING"

    with pytest.raises(AssertionError, match="still non-terminal after the declared"):
        assert_retryable_failures_reach_terminal_at_the_bound(reference_bounded_retry_binding(_NeverExhausts()))


def test_a_retry_lifecycle_that_exhausts_early_fails_the_bound() -> None:
    class _ExhaustsEarly(InMemoryRetryLifecycle):
        def run_once(self, operation_id: int) -> None:
            row = self.rows[operation_id]
            row.executions += 1
            row.state = "TERMINAL"

    with pytest.raises(AssertionError, match="became terminal after 1 execution"):
        assert_retryable_failures_reach_terminal_at_the_bound(reference_bounded_retry_binding(_ExhaustsEarly()))


def test_a_terminal_operation_that_runs_again_fails_stability() -> None:
    class _ReplaysTerminal(InMemoryRetryLifecycle):
        def run_once(self, operation_id: int) -> None:
            row = self.rows[operation_id]
            row.executions += 1
            if row.executions >= self.max_executions:
                row.state = "TERMINAL"
            else:
                row.state = "WAITING"

    with pytest.raises(AssertionError, match="terminal retry changed observable state"):
        assert_terminal_retry_is_a_no_op(reference_bounded_retry_binding(_ReplaysTerminal()))


def test_the_binding_guard_rejects_a_test_module_runner() -> None:
    lifecycle = InMemoryRetryLifecycle()

    def local_run_once(operation_id: int) -> None:
        lifecycle.rows[operation_id].state = "TERMINAL"

    with pytest.raises(AssertionError, match="run_once.*references no production"):
        assert_retry_runner_is_production_bound(reference_bounded_retry_binding(lifecycle, run_once=local_run_once))


def test_a_test_authored_due_selection_cannot_certify_retry_selection() -> None:
    lifecycle = InMemoryRetryLifecycle()
    binding = reference_bounded_retry_binding(lifecycle)

    def local_due_work() -> list[int]:
        return [operation_id for operation_id in lifecycle.rows if not lifecycle.is_terminal(operation_id)]

    with pytest.raises(AssertionError, match="due_work.*references no production"):
        assert_bounded_retry_contract(replace(binding, due_work=local_due_work))


def test_a_copied_due_predicate_cannot_certify_retry_selection() -> None:
    lifecycle = InMemoryRetryLifecycle()
    binding = reference_bounded_retry_binding(lifecycle)

    class _CopiedSelection:
        def filter(self, *, state: str) -> list[int]:
            return [operation_id for operation_id, row in lifecycle.rows.items() if row.state == state]

    copied_selection = _CopiedSelection()

    def copied_due_work() -> list[int]:
        return copied_selection.filter(state="DUE")

    with pytest.raises(AssertionError, match="due_work.*authors production semantics"):
        assert_bounded_retry_contract(replace(binding, due_work=copied_due_work))


def test_a_test_authored_retry_transition_is_rejected() -> None:
    lifecycle = InMemoryRetryLifecycle()

    def copied_run_once(operation_id: int) -> None:
        lifecycle.rows.update({operation_id: lifecycle.rows[operation_id]})

    with pytest.raises(AssertionError, match="run_once.*authors production semantics"):
        assert_retry_runner_is_production_bound(reference_bounded_retry_binding(lifecycle, run_once=copied_run_once))


def test_state_progress_without_a_failure_attempt_fails_the_positive_control() -> None:
    class _SkipsFailingDependency(InMemoryRetryLifecycle):
        def __init__(self) -> None:
            super().__init__()
            self.runs: dict[int, int] = {}

        def make_failing(self) -> int:
            operation_id = super().make_failing()
            self.runs[operation_id] = 0
            return operation_id

        def run_once(self, operation_id: int) -> None:
            row = self.rows[operation_id]
            if row.state != "DUE":
                return
            self.runs[operation_id] += 1
            row.state = "TERMINAL" if self.runs[operation_id] >= self.max_executions else "WAITING"

    with pytest.raises(AssertionError, match="failing dependency recorded 0 new attempts"):
        assert_retryable_failures_reach_terminal_at_the_bound(
            reference_bounded_retry_binding(_SkipsFailingDependency())
        )


def test_advancing_time_cannot_invent_a_failure_attempt() -> None:
    class _AdvanceInventsAttempt(InMemoryRetryLifecycle):
        def __init__(self) -> None:
            super().__init__()
            self.runs: dict[int, int] = {}

        def make_failing(self) -> int:
            operation_id = super().make_failing()
            self.runs[operation_id] = 0
            return operation_id

        def run_once(self, operation_id: int) -> None:
            row = self.rows[operation_id]
            if row.state != "DUE":
                return
            self.runs[operation_id] += 1
            row.executions += 1
            row.state = "TERMINAL" if self.runs[operation_id] >= self.max_executions else "WAITING"

        def advance_to_due(self, operation_id: int) -> None:
            self.rows[operation_id].executions += 1
            super().advance_to_due(operation_id)

    with pytest.raises(AssertionError, match="advance_to_due.*changed the failure-attempt count"):
        assert_retryable_failures_reach_terminal_at_the_bound(reference_bounded_retry_binding(_AdvanceInventsAttempt()))


def test_terminal_attempt_is_detected_even_when_the_state_observation_is_narrow() -> None:
    class _HiddenTerminalAttempt(InMemoryRetryLifecycle):
        def run_once(self, operation_id: int) -> None:
            row = self.rows[operation_id]
            row.executions += 1
            if row.state == "DUE":
                row.state = "TERMINAL" if row.executions >= self.max_executions else "WAITING"

    lifecycle = _HiddenTerminalAttempt()

    def observe_state(operation_id: int) -> str:
        return lifecycle.rows[operation_id].state

    with pytest.raises(AssertionError, match="terminal retry recorded another failure attempt"):
        assert_terminal_retry_is_a_no_op(reference_bounded_retry_binding(lifecycle, observe=observe_state))
