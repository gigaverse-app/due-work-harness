"""
Process histories: the verdict for deaths a host arranges itself.

A real child process is the demos' job; here ``run`` stands in for one by
leaving the reference ledger exactly as a process that died at each point
would, which is all the verdict can see anyway.
"""

import pytest

from due_work_harness.process_histories import ProcessHistory, assert_process_deaths_converge
from due_work_harness.references import in_memory_handoffs as ref


def _run(point: str | None, *, notify: str = "notify") -> tuple[int, int]:
    attempt = ref.running_attempt()
    if point is None:
        ref.complete_notifying(attempt)
        return attempt, 0
    if point == "after_send":
        getattr(ref.RECIPIENT, notify)(attempt)
    return attempt, 1


def _history(points: tuple[str, ...]) -> ProcessHistory[int, tuple[str, int]]:
    return ProcessHistory(
        name="completion",
        initial=("absent", 0),
        run=_run,
        observe=ref.status_and_notifications,
        recover=ref.recover_after_death,
        death_points=points,
    )


def test_a_death_before_the_send_converges(ledger_host: object) -> None:
    assert_process_deaths_converge(_history(("before_send",)))


def test_a_death_after_the_send_is_a_repeat(ledger_host: object) -> None:
    with pytest.raises(AssertionError, match=r"'died at after_send': \('complete', 2\)"):
        assert_process_deaths_converge(_history(("before_send", "after_send")))


def test_a_history_with_no_death_points_is_refused(ledger_host: object) -> None:
    with pytest.raises(AssertionError, match="no death points"):
        assert_process_deaths_converge(_history(()))
