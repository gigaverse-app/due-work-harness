"""
Process histories: the verdict for deaths a host arranges itself.

A real child process is the demos' job; here ``run`` stands in for one by
leaving the reference ledger exactly as a process that died at each point
would, which is all the verdict can see anyway.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pytest_obligation.crash_histories import HistoriesDiverged
from pytest_obligation.process_histories import (
    ProcessHistory,
    assert_pinned_process_outcomes,
    assert_process_deaths_converge,
    fault_environment,
    fault_fires,
    fault_happened,
)
from pytest_obligation.references import in_memory_handoffs as ref


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
    with pytest.raises(AssertionError, match="no death or failure points"):
        assert_process_deaths_converge(_history(()))


def _surviving_run(point: str | None) -> tuple[int, int]:
    # A failure the program survives: the notification is sent twice, and the run reports the fault happened.
    attempt = ref.running_attempt()
    ref.complete_notifying(attempt)
    if point == "the recipient's reply was lost":
        ref.RECIPIENT.notify(attempt)
        return attempt, 1
    return attempt, 0


def test_a_failure_point_is_labelled_by_its_own_name(ledger_host: object) -> None:
    history = ProcessHistory(
        name="completion",
        initial=("absent", 0),
        run=_surviving_run,
        observe=ref.status_and_notifications,
        recover=ref.recover_after_death,
        death_points=(),
        failure_points=("the recipient's reply was lost",),
    )
    with pytest.raises(AssertionError, match=r"\"the recipient's reply was lost\": \('complete', 2\)"):
        assert_process_deaths_converge(history)
    assert_pinned_process_outcomes(
        history, delivered=("complete", 1), outcomes={"the recipient's reply was lost": ("complete", 2)}
    )


def test_a_failure_that_never_happened_is_not_a_history(ledger_host: object) -> None:
    history = ProcessHistory(
        name="completion",
        initial=("absent", 0),
        run=lambda point: _surviving_run(None),
        observe=ref.status_and_notifications,
        recover=ref.recover_after_death,
        death_points=(),
        failure_points=("the recipient's reply was lost",),
    )
    with pytest.raises(AssertionError, match="never interrupted"):
        assert_pinned_process_outcomes(history, delivered=("complete", 1), outcomes={})


def test_a_fault_fires_only_where_the_parent_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {**os.environ, **fault_environment("after_send")})
    assert not fault_fires("before_send")
    assert fault_fires("after_send")
    # With no marker there is no once-guard: the child asked is the child that fails.
    assert fault_fires("after_send")


def test_a_marker_makes_a_fault_fire_once_across_processes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "fault"
    monkeypatch.setattr(os, "environ", {**os.environ, **fault_environment("after_send", marker)})
    assert not fault_happened(marker)
    assert fault_fires("after_send")
    assert not fault_fires("after_send")
    assert fault_happened(marker)


CHILD = "from pytest_obligation.process_histories import die_here\ndie_here('after_send')\nprint('lived')\n"


@pytest.mark.parametrize(("point", "status", "output"), [("after_send", 1, ""), (None, 0, "lived\n")])
def test_a_child_dies_where_it_is_told(point: str | None, status: int, output: str) -> None:
    child = subprocess.run(
        [sys.executable, "-c", CHILD], env={**os.environ, **fault_environment(point)}, capture_output=True, text=True
    )
    assert (child.returncode, child.stdout) == (status, output)


def test_an_observation_that_differs_between_clean_runs_is_refused_not_a_divergence(ledger_host: object) -> None:
    # A per-run identifier (a task id, a fresh row) makes every history "diverge"; a known gap must not accept that.
    history = _history(("before_send",)).model_copy(
        update={"observe": lambda attempt: (attempt, *ref.status_and_notifications(attempt))}
    )
    with pytest.raises(AssertionError, match="not deterministic") as raised:
        assert_process_deaths_converge(history)
    assert not isinstance(raised.value, HistoriesDiverged)
