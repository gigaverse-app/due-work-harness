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

Some faults the program survives: a hook that raises, a broker that refuses a
publish. Name those in ``failure_points``; the child fails there and lives on,
and ``run`` reports a non-zero status when the failure happened, so a point the
program never reached is caught as a run that was never interrupted. Their
histories are labelled with the point's own name, deaths with ``died at``.
"""

from collections.abc import Callable
from typing import Any

from due_work_harness.binding import INVOCATION_AUTHORING_OPERATIONS, assert_binding_reaches_production
from due_work_harness.crash_histories import HistoryRun, assert_histories_converge, assert_runs_match_table
from due_work_harness.models import HarnessModel


class ProcessHistory[HandleT, ObservationT](HarnessModel):
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
    """Normal operation first, then one run per death point, then one per failure point."""
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
    points = (*history.death_points, *history.failure_points)
    return [normal, *(_run(history, point) for point in points)]


def assert_process_deaths_converge(history: ProcessHistory[Any, Any]) -> None:
    """A death at every named point, then production's own restart, reaches normal operation's outcome."""
    assert_histories_converge(history.name, process_histories(history))


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
