"""
Crash histories where the worker is a real process that really dies.

:mod:`due_work_harness.crash_histories` kills a worker in-process, which needs
a host that can intercept commits on the worker's connection. Some systems do
their work where the test cannot reach: a workflow engine's executor threads, a
second connection pool, a framework that owns its own event loop. For those the
honest death is the operating system's: run the real program in a child
process, let it die at a named point (``os._exit`` from a seam, before or after
an external call, after a commit), then start the program again the way
production would and observe what the product ends up with.

The harness owns the parts that decide the verdict — normal operation defines
the outcome, every death must actually happen, recovery must bind production
code, and when everything converges recovery must have changed something — and
the adopter owns the program and its death points. The adopter's child entry
point reads the death point it was given and dies there; the harness never
guesses where a program can die.

::

    ProcessHistory(
        name="place order",
        initial=(None, 0),
        run=place_order_in_a_child_process,     # (death_point | None) -> (handle, exit code)
        observe=order_status_and_notifications,  # handle -> observation
        recover=restart_the_application,         # handle -> None; production's own startup
        death_points=("after_order", "before_send", "after_send"),
    )

A child that exits with status 0 is taken to have run normally; any other
status is a death. Normal operation (``None``) must exit 0, and each death
point must not.

The child and the parent speak one protocol, so an adopter writes no
environment plumbing: the parent passes :func:`fault_environment` to the child,
and the child calls :func:`die_here` at each death point, or asks
:func:`fault_fires` at a failure point, then fails its own way. A marker file
makes each fault fire once across every process the child starts (a worker's
pool, a restarted executor), and :func:`fault_happened` tells the parent it did.

Some faults the program survives: a hook that raises, a broker that refuses a
publish. Name those in ``failure_points``; the child fails there and lives on,
and ``run`` reports a non-zero status when the failure happened, so a point the
program never reached is caught as a run that was never interrupted. Their
histories are labelled with the point's own name, deaths with ``died at``.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

from due_work_harness.binding import INVOCATION_AUTHORING_OPERATIONS, assert_binding_reaches_production
from due_work_harness.crash_histories import (
    Findings,
    HistoryRun,
    assert_findings_hold,
    assert_histories_converge,
    assert_normal_operation_repeats,
    assert_runs_match_table,
)
from due_work_harness.models import HarnessModel

#: The child's environment: the point to fail at, and the marker recording that it did.
FAULT_VARIABLE = "DUE_WORK_FAULT"
FAULT_MARKER_VARIABLE = "DUE_WORK_FAULT_MARKER"


def fault_environment(point: str | None, marker: Path | None = None) -> dict[str, str]:
    """PARENT: the environment telling a child to fail at ``point`` (``None``: run normally)."""
    return {FAULT_VARIABLE: point or "", FAULT_MARKER_VARIABLE: str(marker) if marker is not None else ""}


def fault_fires(point: str) -> bool:
    """
    CHILD: whether to fail at ``point`` now.

    True when the parent asked for ``point`` and, with a marker, this is the
    first time any process of the child got here: the marker is created
    atomically, so a worker's pool children cannot all fail.
    """
    if os.environ.get(FAULT_VARIABLE) != point:
        return False
    marker = os.environ.get(FAULT_MARKER_VARIABLE)
    if not marker:
        return True
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def die_here(point: str) -> None:
    """CHILD: FAULT INJECTION. Exit at once, as a killed process would, when the parent asked to die at ``point``."""
    if fault_fires(point):
        os._exit(1)


def fault_happened(marker: Path) -> bool:
    """PARENT: whether the child reached the fault it was given (it needs the marker to tell)."""
    return marker.exists()


HandleT = TypeVar("HandleT")
ObservationT = TypeVar("ObservationT")


class ProcessHistory(HarnessModel, Generic[HandleT, ObservationT]):
    """One production transition run in a child process, with the points where it may die."""

    name: str

    #: The observation of work that does not exist yet: the "before" of every run.
    initial: ObservationT

    #: FAULT INJECTION: run the real program in a child process, told to die at
    #: the given point (``None`` for normal operation). Returns the handle to
    #: observe and the child's exit status.
    run: Callable[[str | None], tuple[HandleT, int]]

    #: OBSERVE: product and obligation state, including what external systems saw.
    observe: Callable[[HandleT], ObservationT]

    #: REAL PRODUCTION: what happens after any death — the application starting
    #: again and recovering, until the handle's work is settled or a bound passes.
    recover: Callable[[HandleT], object]

    #: The named points the child's entry point knows how to die at.
    death_points: tuple[str, ...]

    #: The named faults the child survives: it fails there and lives on.
    failure_points: tuple[str, ...] = ()

    #: Optional: what the histories leave, checked in the same run as the verdict.
    findings: Findings | None = None


def _run(history: ProcessHistory[Any, Any], point: str | None) -> HistoryRun:
    handle, status = history.run(point)
    midway = history.observe(handle)
    history.recover(handle)
    if point is None:
        label = "normal operation"
    elif point in history.failure_points:
        label = point
    else:
        label = f"died at {point}"
    return HistoryRun(
        label=label,
        before=history.initial,
        midway=midway,
        after=history.observe(handle),
        interrupted=status != 0,
    )


def process_histories(history: ProcessHistory[Any, Any]) -> list[HistoryRun]:
    """Normal operation first (run twice: it must repeat), then one run per death point, then per failure point."""
    assert history.death_points or history.failure_points, (
        f"{history.name}: no death or failure points, so no history could be interrupted"
    )
    assert_binding_reaches_production(
        adopter=history.name,
        field="recover",
        binding=history.recover,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the application's own startup and recovery",
    )
    normal = _run(history, None)
    assert not normal.interrupted, (
        f"{history.name}: normal operation exited abnormally, so it cannot define the outcome. Fix the child "
        f"entry point before judging its deaths"
    )
    assert_normal_operation_repeats(history.name, normal, _run(history, None))
    points = (*history.death_points, *history.failure_points)
    return [normal, *(_run(history, point) for point in points)]


def assert_process_deaths_converge(history: ProcessHistory[Any, Any]) -> None:
    """
    A death or failure at every named point, then production's own restart, reaches normal operation's outcome.

    When the history declares its :class:`~due_work_harness.crash_histories.Findings`,
    the same runs are first held to that table.
    """
    runs = process_histories(history)
    assert_findings_hold(history.name, runs, history.findings)
    assert_histories_converge(history.name, runs)


def assert_pinned_process_outcomes(
    history: ProcessHistory[Any, Any], *, delivered: Any, outcomes: dict[str, Any]
) -> list[HistoryRun]:
    """
    What each death and failure of a process history leaves, pinned: a findings table.

    The same verdict as :func:`~due_work_harness.crash_histories.assert_pinned_outcomes`:
    normal operation reaches ``delivered``, each named history reaches its
    entry, every other history reaches ``delivered``. Returns the runs.
    """
    runs = process_histories(history)
    uninterrupted = [run.label for run in runs[1:] if not run.interrupted]
    assert not uninterrupted, f"{history.name}: these histories were never interrupted: {uninterrupted}"
    assert_runs_match_table(history.name, runs, delivered=delivered, outcomes=outcomes)
    return runs
