"""
django-tasks integration: the database backend's worker, as recovery and as the transition under test.

django-tasks-db stores each enqueued task as a row, so a task enqueued inside
the product transaction commits or rolls back with it: there is no message
separate from the database to lose. Its worker, ``manage.py db_worker``, claims
a READY row, marks it RUNNING, runs it and records the outcome. What an
application using it still owes itself:

* **enqueuing in the same transaction** as the state that owes the task — a
  task enqueued from ``transaction.on_commit`` is lost with a process that dies
  between the commit and the callback (crash histories find this);
* **recovering a task whose worker died** — the worker selects only READY
  rows, so a task left RUNNING by a dead worker is never run again.

:func:`worker_contract` is django-tasks-db's own contract with its worker, for
any adopter: the adopter enqueues one of its tasks and says how to see the
task's effect; the dispositions, the known gaps and the proofs are the
framework's. Its crash histories fail each ``task_started`` and
``task_finished`` receiver when the host's ``receiver_breaker`` names those
signals.

Helpers here import Django and django-tasks-db lazily, so importing this module
never requires them.
"""

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from pytest_obligation.contract import (
    Adoption,
    Claim,
    ExtraProof,
    KnownGap,
    NotApplicable,
    ObligationContract,
    Profile,
)
from pytest_obligation.crash_histories import CallableDelivery, Delivery, ExternalCall, Findings, HandoffHistory
from pytest_obligation.gap_probes import MissingReclaim
from pytest_obligation.integrations import task_queues
from pytest_obligation.integrations.task_queues import TaskOutcome, application_admission, application_gate
from pytest_obligation.profiles.durable_retention import Retention


def db_worker_once(*, queues: str = "*", backend: str = "default") -> Callable[..., None]:
    """
    Recovery for a project on django-tasks-db: one ``manage.py db_worker --batch`` pass until no task is ready.

    The same pass is a handoff's transition when the worker itself is under
    test (it ignores the history's handle). ``db_worker`` installs its own SIGINT
    and SIGTERM handlers for graceful shutdown; they are restored afterwards, so
    the test process keeps its own.
    """

    def run(*_handle: object) -> None:
        from django.core.management import call_command

        with task_queues.keeping_signal_handlers():
            call_command(
                "db_worker",
                "--batch",
                "--no-startup-delay",
                "--no-reload",
                "--queue-name",
                queues,
                "--backend",
                backend,
                verbosity=0,
            )

    return run


def strand_with_dead_worker(task_id: str, *, worker_id: str = "a-worker-that-died") -> str:
    """FAULT INJECTION: make ``task_id`` look claimed by a worker that then died — RUNNING, started, owned by it."""
    from django.utils import timezone
    from django_tasks_db.models import DBTaskResult

    DBTaskResult.objects.filter(id=task_id).update(status="RUNNING", started_at=timezone.now(), worker_ids=[worker_id])
    return task_id


def tasks_run_by(tick: Callable[[], object]) -> list[str]:
    """The ids of the tasks ``tick`` started, as django-tasks-db's own ``task_started`` signal reports them."""
    from django_tasks_db.compat import task_started

    started: list[str] = []

    def record(sender: object, task_result: Any, **kwargs: object) -> None:
        started.append(str(task_result.id))

    task_started.connect(record, weak=False)
    try:
        tick()
    finally:
        task_started.disconnect(record)
    return started


def _status_of(task_id: str) -> str:
    from django_tasks_db.models import DBTaskResult

    return DBTaskResult.objects.get(id=task_id).status


def worker_history(
    *,
    enqueue: Callable[[], str],
    effect: Callable[[str], Any],
    external_calls: Sequence[ExternalCall] = (),
    name: str = "the worker runs a task",
    findings: Findings | None = None,
) -> HandoffHistory[str, TaskOutcome]:
    """
    The worker itself as the transition: db_worker runs one task the adopter enqueued.

    ``enqueue`` enqueues one of the adopter's tasks and returns its id;
    ``effect`` observes what the task did (for example, how many times a CDN was
    asked to purge its URL). See :func:`pytest_obligation.integrations.task_queues.worker_history`.
    """
    return task_queues.worker_history(
        enqueue=enqueue,
        effect=effect,
        status_of=_status_of,
        run_worker=db_worker_once(),
        external_calls=external_calls,
        name=name,
        findings=findings,
    )


def dead_worker_reclaim(*, enqueue: Callable[[], str]) -> MissingReclaim:
    """The gap probe: a task stranded RUNNING by a dead worker is run again by the next db_worker pass."""
    return MissingReclaim(
        make_stranded=lambda: strand_with_dead_worker(enqueue()),
        dispatched_by_one_tick=lambda: tasks_run_by(db_worker_once()),
    )


def retention(*, enqueue: Callable[[], str], min_age_days: int = 14) -> Retention:
    """
    Profile D bound to django-tasks-db's ``prune_db_task_results`` command.

    The owed row is a task enqueued and never run; the prunable one is a task
    ``db_worker`` ran to completion. Both are aged past ``min_age_days`` by
    moving their timestamps back: the owed one must survive the pass anyway.
    """
    old = timedelta(days=min_age_days + 1)

    def owed() -> str:
        from django.utils import timezone
        from django_tasks_db.models import DBTaskResult

        task_id = enqueue()
        DBTaskResult.objects.filter(id=task_id).update(enqueued_at=timezone.now() - old)
        return task_id

    def finished() -> str:
        from django.utils import timezone
        from django_tasks_db.models import DBTaskResult

        task_id = enqueue()
        db_worker_once()()
        DBTaskResult.objects.filter(id=task_id).update(finished_at=timezone.now() - old)
        return task_id

    def prune() -> None:
        from django.core.management import call_command

        call_command("prune_db_task_results", "--queue-name", "*", "--min-age-days", str(min_age_days), verbosity=0)

    def still_exists(task_id: str) -> bool:
        from django_tasks_db.models import DBTaskResult

        return DBTaskResult.objects.filter(id=task_id).exists()

    return Retention(
        name="django-tasks-db prune_db_task_results",
        make_non_terminal=owed,
        make_prunable=finished,
        run_retention=prune,
        still_exists=still_exists,
    )


#: What django-tasks-db 0.13's worker lacks, for any adopter's contract.
NO_RECLAIM = (
    "the worker selects only READY tasks, so a task left RUNNING by a worker that died is never run again "
    "(django-tasks-db#5)"
)
NO_LEASE = "a claim holds no lease and no heartbeat: nothing can tell a live worker's task from a dead one's"
NO_ATTEMPT_RECORD = (
    "a task is marked RUNNING before its effect and SUCCESSFUL after it, with nothing in between: after a death "
    "the row cannot tell whether the effect happened"
)
#: Django's task framework has no retries: a task that fails stays failed.
RUNS_ONCE = "Django's task framework runs a task once: it is never retried"
WORKER_HISTORY_GAP = (
    "a worker that dies after claiming the task, or after its external call, leaves it RUNNING forever; a "
    "task_started receiver that raises marks the task FAILED before it runs, and nothing retries it; and a "
    "task_finished receiver that raises after the task ran rewrites its SUCCESSFUL record as FAILED, because "
    "run_task sends task_finished inside the try that records a failure (django-tasks-db#62)"
)


def worker_contract(
    *,
    name: str,
    enqueue: Callable[[], str],
    effect: Callable[[str], Any],
    external_calls: Sequence[ExternalCall] = (),
    delivery: Delivery | None = None,
    min_age_days: int = 14,
    findings: Findings | None = None,
) -> ObligationContract:
    """
    django-tasks-db's contract with its worker, bound to one of the adopter's tasks.

    Retention is claimed and proven against ``prune_db_task_results``; the
    worker's missing reclaim, lease and attempt record are declared known gaps,
    each a strict xfail that a fix upstream flips; and the worker's own crash
    histories — deaths after each commit and after the task's external calls,
    and each worker signal receiver failing — are a legacy handoff gap. The
    contract is transactional: the worker commits for real.
    """
    history = worker_history(enqueue=enqueue, effect=effect, external_calls=external_calls, findings=findings)
    return ObligationContract(
        name=name,
        adoption=Adoption.LEGACY,
        transactional=True,
        profiles={
            Profile.A: KnownGap(NO_RECLAIM),
            Profile.B: KnownGap(NO_LEASE),
            Profile.C: KnownGap(NO_ATTEMPT_RECORD),
            Profile.D: Claim(),
            Profile.E: task_queues.settled_by_one_worker("task"),
            Profile.F: task_queues.the_obligation_is_the("task row"),
            Profile.H: NotApplicable("django-tasks-db never replays a task"),
            Profile.J: NotApplicable(RUNS_ONCE),
            Profile.G: application_gate("django-tasks-db"),
            Profile.I: application_admission("django-tasks-db"),
        },
        retention=lambda: retention(enqueue=enqueue, min_age_days=min_age_days),
        handoffs=(history,),
        handoff_delivery=delivery or CallableDelivery(name=f"{name}: db_worker", recover=db_worker_once()),
        handoff_gaps={history.name: WORKER_HISTORY_GAP},
        extras=(
            ExtraProof(
                name="a task whose worker died is run again by what django-tasks-db runs",
                run=dead_worker_reclaim(enqueue=enqueue),
                transactional=True,
                gap=NO_RECLAIM,
            ),
        ),
    )
