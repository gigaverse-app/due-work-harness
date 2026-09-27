"""
Procrastinate integration: its worker as recovery, and the faults it must survive.

Procrastinate keeps jobs in a PostgreSQL table, so a job deferred inside the
product transaction commits or rolls back with it; there is no message separate
from the database to lose. What a Procrastinate application still owes itself:

* **deferring in the same transaction** as the state that owes the job — a job
  deferred in autocommit after the product row commits is lost with a worker
  that dies in between (crash histories find this);
* **reclaiming jobs whose worker died** — a job left ``doing`` by a dead worker
  is not fetched again until something retries it; Procrastinate documents a
  periodic task for this (``retry_stalled_jobs``), which an application must
  define and schedule itself.

Helpers here bind those facts to the harness. They import Procrastinate lazily,
so importing this module never requires it.

Running the worker in a test: with the Django integration, run it the way
production does, through ``manage.py procrastinate worker``
(:func:`django_worker_once`). Procrastinate's Django connector is documented as
unsuitable for workers; ``App.run_worker`` on it can mark a job ``doing`` and
return before running it, which leaves every history agreeing on unfinished
work.
"""

from collections.abc import Callable, Iterable, Sequence
from datetime import timedelta
from typing import Any


def django_worker_once(queues: Sequence[str]) -> Callable[[], None]:
    """Recovery for a Django project: one ``manage.py procrastinate worker`` pass until the queues are idle."""

    def run() -> None:
        from django.core.management import call_command

        call_command("procrastinate", "worker", "--queues", ",".join(queues), "--one-shot", "--no-listen-notify")

    return run


def worker_once(app: Any, queues: Sequence[str]) -> Callable[[], None]:
    """Recovery for an app with a worker-suitable connector: run until the queues are idle."""

    def run() -> None:
        app.run_worker(queues=list(queues), wait=False, install_signal_handlers=False, listen_notify=False)

    return run


def strand_with_dead_worker(cursor: Any, job_id: int, *, heartbeat_age: timedelta = timedelta(hours=1)) -> int:
    """
    FAULT INJECTION: make ``job_id`` look fetched by a worker that then died.

    Inserts a worker whose last heartbeat is ``heartbeat_age`` old and marks the
    job ``doing`` under it, with one attempt spent. ``cursor`` is any DB-API
    cursor on the application's database. Returns the dead worker's id.
    """
    cursor.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (NOW() - %s) RETURNING id",
        [heartbeat_age],
    )
    (dead_worker,) = cursor.fetchone()
    cursor.execute(
        "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s, attempts = attempts + 1 WHERE id = %s",
        [dead_worker, job_id],
    )
    return dead_worker


def attempts_by_job(cursor: Any) -> dict[int, int]:
    """OBSERVE: every job's attempt count, as Procrastinate itself records it."""
    cursor.execute("SELECT id, attempts FROM procrastinate_jobs")
    return dict(cursor.fetchall())


def redispatched_by(tick: Callable[[], object], attempts: Callable[[], dict[int, int]]) -> set[int]:
    """OBSERVE: the jobs a tick ran again, judged by attempts rising during it."""
    before = attempts()
    tick()
    return {job_id for job_id, count in attempts().items() if count > before.get(job_id, 0)}


async def retry_stalled_jobs(app: Any) -> None:
    """Procrastinate's documented recipe for reclaiming jobs whose worker stopped heartbeating."""
    for job in await app.job_manager.get_stalled_jobs():
        await app.job_manager.retry_job(job)


def periodic_tasks(app: Any) -> Iterable[str]:
    """The task names the app's periodic registry schedules, for schedule evidence."""
    return [periodic.task.name for periodic in app.periodic_registry.periodic_tasks.values()]


def procrastinate_periodic_evidence(app: Any, task_name: str) -> Callable[[], None]:
    """Schedule evidence: ``task_name`` is registered as a periodic task on ``app``."""

    def evidence() -> None:
        scheduled = set(periodic_tasks(app))
        assert task_name in scheduled, (
            f"{task_name!r} is not a periodic task of this procrastinate app ({sorted(scheduled)}), so nothing "
            f"runs it on a schedule"
        )

    return evidence
