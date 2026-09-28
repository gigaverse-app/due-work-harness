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

It imports no framework: the integrations for django-tasks-db and RQ bind it.
"""

from collections.abc import Callable, Sequence
from typing import Any

from due_work_harness.crash_histories import ExternalCall, HandoffHistory
from due_work_harness.models import HarnessModel


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
    )


__all__ = ["TaskOutcome", "worker_history"]
