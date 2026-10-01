"""
What every task-queue integration shares: its worker as the transition under test.

A task queue records a task, and a worker later claims it, runs it and records
how it ended. The worker is where a death, a lost reply or a failing hook turns
into a task that ran but reads as failed, a task run twice, or a task never run.
:func:`worker_history` makes one pass of the queue's real worker the transition
of a crash history: the adopter enqueues one of its own tasks and says how to see
the task's effect, the integration says how to run the worker and read the
status it recorded. The observation is both, so a history in which the effect
happened but the record says otherwise diverges.

:func:`keeping_signal_handlers` is the other thing every in-process worker
needs: a real worker installs its own shutdown handlers, and the test process
must get its own back afterwards, even after a death.

It imports no framework: the integrations for django-tasks-db and RQ bind it.
"""

import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from pytest_obligation.contract import Decline, NotApplicable
from pytest_obligation.crash_histories import ExternalCall, Findings, HandoffHistory
from pytest_obligation.models import HarnessModel

#: The shutdown signals task-queue workers install their own handlers for.
WORKER_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGQUIT") if hasattr(signal, name))


@contextmanager
def keeping_signal_handlers(signals: Sequence[int] = WORKER_SIGNALS) -> Iterator[None]:
    """Run a worker in process and restore the test process's own handlers for ``signals`` afterwards."""
    handlers = {signum: signal.getsignal(signum) for signum in signals}
    try:
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def settled_by_one_worker(unit: str) -> NotApplicable:
    """Profile E for a queue: one worker settles each unit of work, so no two results race."""
    return NotApplicable(f"one worker settles each {unit}; no two results race to write it")


def the_obligation_is_the(unit: str) -> NotApplicable:
    """Profile F for a queue: the obligation is the queued unit itself."""
    return NotApplicable(f"the obligation is the {unit} itself, not a fact derived from product state")


def replay_safety_is_the_functions(unit: str, library: str, *, runs_again: str) -> Decline:
    """Replay safety for an at-least-once queue: it belongs to each unit's function, not to the queue."""
    return Decline(
        f"a {unit} can run more than once ({runs_again}), so rerunning it must be safe, but that is a property "
        f"of each {unit}'s function, not of {library}"
    )


def application_admission(library: str) -> Decline:
    """Profile I belongs to the command admitting product intent with the queued work."""
    return Decline(
        f"{library}'s worker receives already admitted work; it cannot make an application's "
        "product transaction atomic with enqueue. Bind Profile I on the application's command contract."
    )


def application_gate(library: str) -> Decline:
    """Worker execution alone cannot certify a product prerequisite or dependency release."""
    return Decline(
        f"{library}'s worker contract has no application readiness predicate; dependencies and product "
        "gates need their own ExecutionGate binding, including release with its notification lost."
    )


class TaskOutcome(HarnessModel):
    """What the queue recorded for a task, and what its effect was."""

    status: str
    effect: Any


def worker_history(
    *,
    enqueue: Callable[[], str],
    effect: Callable[[str], Any],
    status_of: Callable[[str], str],
    run_worker: Callable[..., object],
    external_calls: Sequence[ExternalCall] = (),
    name: str = "the worker runs a task",
    findings: Findings | None = None,
) -> HandoffHistory[str, TaskOutcome]:
    """
    The worker itself as the transition: ``run_worker`` runs the task ``enqueue`` enqueued.

    ``enqueue`` enqueues one of the adopter's tasks and returns its id;
    ``effect`` observes what the task did (for example, how many messages it
    sent); ``status_of`` reads the status the queue recorded for it.
    """

    def observe(task_id: str) -> TaskOutcome:
        return TaskOutcome(status=status_of(task_id), effect=effect(task_id))

    return HandoffHistory(
        name=name,
        arrange=enqueue,
        transition=run_worker,
        observe=observe,
        external_calls=tuple(external_calls),
        findings=findings,
    )


__all__ = [
    "WORKER_SIGNALS",
    "TaskOutcome",
    "keeping_signal_handlers",
    "replay_safety_is_the_functions",
    "settled_by_one_worker",
    "the_obligation_is_the",
    "worker_history",
]
