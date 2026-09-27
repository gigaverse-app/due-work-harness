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

from due_work_harness.profiles.bounded_ownership import FencedOwnership
from due_work_harness.profiles.durable_retention import Retention


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


def _run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


#: Tokens stand for procrastinate's only owner identity, the worker id.
_TOKENS = "due-work-harness procrastinate worker"


def ownership(
    app: Any,
    *,
    defer: Callable[[], int],
    queue: str,
    stalled_after: timedelta = timedelta(seconds=30),
) -> FencedOwnership:
    """
    Profile B bound to procrastinate's own ownership: its workers, their heartbeats, and the documented reclaim.

    ``defer`` defers one job on ``queue`` through the application's own task and
    returns its id. A claim registers a worker and fetches a job exactly as
    procrastinate's worker does (``JobManager.register_worker`` then
    ``fetch_job``); the token is that worker's id, the only owner identity
    procrastinate records. The fenced write is ``finish_job``; renewing the
    lease is the worker's heartbeat; reclaiming a stalled job is procrastinate's
    documented recipe (``get_stalled_jobs`` then ``retry_job``), applied to that
    one job. The app's connector must run synchronously in the calling thread,
    as the Django connector does.
    """
    from uuid import NAMESPACE_URL, UUID, uuid5

    from procrastinate import jobs

    manager = app.job_manager
    workers: dict[UUID, int] = {}

    def token_for(worker_id: int) -> UUID:
        token = uuid5(NAMESPACE_URL, f"{_TOKENS} {worker_id}")
        workers[token] = worker_id
        return token

    def claim() -> tuple[int, UUID] | None:
        worker_id = _run(manager.register_worker())
        job = _run(manager.fetch_job(queues=[queue], worker_id=worker_id))
        return None if job is None else (job.id, token_for(worker_id))

    def finish(job_id: int, token: UUID) -> bool:
        # procrastinate's finish_job takes no owner: the token cannot reach it.
        del token
        try:
            _run(manager.finish_job_by_id_async(job_id=job_id, status=jobs.Status.SUCCEEDED, delete_job=False))
        except Exception:
            return False
        return True

    def heartbeat(job_id: int, token: UUID) -> bool:
        # A heartbeat renews a worker, and through it the jobs that worker holds. A stale
        # worker may still send one; it renews only its own row, never a job reclaimed
        # from it, so report whether this job's lease was the one renewed.
        worker_id = workers[token]
        _run(manager.update_heartbeat(worker_id=worker_id))
        owner = app.connector.execute_query_one(
            "SELECT worker_id FROM procrastinate_jobs WHERE id = %(job_id)s", job_id=job_id
        )
        return owner is not None and owner["worker_id"] == worker_id

    def expire(job_id: int) -> None:
        # FAULT INJECTION: the owning worker stopped heartbeating long ago.
        app.connector.execute_query(
            "UPDATE procrastinate_workers SET last_heartbeat = NOW() - INTERVAL '1 day' "
            "WHERE id = (SELECT worker_id FROM procrastinate_jobs WHERE id = %(job_id)s)",
            job_id=job_id,
        )

    def reclaim(job_id: int) -> str | None:
        stalled = _run(manager.get_stalled_jobs(seconds_since_heartbeat=stalled_after.total_seconds()))
        job = next((job for job in stalled if job.id == job_id), None)
        if job is None:
            return None
        _run(manager.retry_job(job))
        return "retried"

    def observe(job_id: int) -> Any:
        # The job's state, its owner, and the owner's heartbeat: procrastinate's lease deadline.
        return app.connector.execute_query_one(
            "SELECT job.status, job.worker_id, worker.last_heartbeat FROM procrastinate_jobs job "
            "LEFT JOIN procrastinate_workers worker ON worker.id = job.worker_id WHERE job.id = %(job_id)s",
            job_id=job_id,
        )

    return FencedOwnership(
        name=f"procrastinate {queue} jobs",
        make_claimable=defer,
        claim=claim,
        fenced_write=finish,
        renew_lease=heartbeat,
        expire_lease=expire,
        reclaim_stalled=reclaim,
        observe=observe,
    )


def retention(
    app: Any,
    *,
    defer: Callable[[], int],
    max_hours: int,
    remove_failed: bool = False,
) -> Retention:
    """
    Profile D bound to procrastinate's own pruning: the builtin ``remove_old_jobs`` task's ``delete_old_jobs``.

    ``defer`` defers one job through the application's own task and returns its
    id. Both rows are made older than ``max_hours`` by moving their events back:
    the owed one must survive the pass anyway, the finished one must go.
    """
    from procrastinate import jobs

    manager = app.job_manager

    def age(job_id: int) -> None:
        # ARRANGE: the job's history is older than the retention window.
        app.connector.execute_query(
            "UPDATE procrastinate_events SET at = at - make_interval(hours => %(hours)s) WHERE job_id = %(job_id)s",
            hours=max_hours + 24,
            job_id=job_id,
        )

    def owed() -> int:
        job_id = defer()
        age(job_id)
        return job_id

    def finished() -> int:
        job_id = defer()
        _run(manager.finish_job_by_id_async(job_id=job_id, status=jobs.Status.SUCCEEDED, delete_job=False))
        age(job_id)
        return job_id

    def prune() -> None:
        _run(manager.delete_old_jobs(nb_hours=max_hours, include_failed=remove_failed))

    def exists(job_id: int) -> bool:
        return app.connector.execute_query_one(
            "SELECT EXISTS (SELECT 1 FROM procrastinate_jobs WHERE id = %(job_id)s) AS present", job_id=job_id
        )["present"]

    return Retention(
        name="procrastinate remove_old_jobs",
        make_non_terminal=owed,
        make_prunable=finished,
        run_retention=prune,
        still_exists=exists,
    )
