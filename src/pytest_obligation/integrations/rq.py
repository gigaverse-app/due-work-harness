"""
RQ integration: its worker as the transition and as recovery, its ownership, and its contract.

RQ keeps jobs in Redis. A worker moves a job from its queue into an
intermediate list, records an execution in the ``StartedJobRegistry`` with a
lease (the heartbeat TTL), runs it, and records how it ended. Recovery is the
maintenance every worker runs on start and every ten minutes: it fails, or
retries when the job has retries left, any job whose lease expired, and fails a
job stuck in the intermediate list for over a minute.

Helpers here bind those facts to the harness:

* :func:`worker_pass` — one burst pass of RQ's in-process ``SimpleWorker``, the
  transition crash histories interrupt (a forking ``Worker`` runs the job in a
  child process the harness cannot see);
* :func:`maintenance_passes` — recovery as RQ performs it: later workers' start
  up maintenance, run with the host's frozen clock an hour apart, so leases and
  the intermediate list's one-minute grace have expired;
* :func:`ownership` — profile B bound to RQ's claim, heartbeat, settlements and
  ``StartedJobRegistry.cleanup``;
* :func:`rq_callback_breaker` — a ``receiver_breaker`` that fails each of a
  job's ``on_success``, ``on_failure`` and ``on_stopped`` callbacks in turn;
* :func:`worker_contract` — RQ's contract with its worker for any adopter.

Configure the host with :func:`pytest_obligation.integrations.redis.redis_host`
on the worker's Redis client, passing ``receiver_breaker=rq_callback_breaker``.
Helpers import RQ lazily, so importing this module never requires it.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pytest_obligation.contract import (
    Adoption,
    Claim,
    Decline,
    ObligationContract,
    Profile,
)
from pytest_obligation.crash_histories import CallableDelivery, Delivery, ExternalCall, Findings, HandoffHistory
from pytest_obligation.faults import CountedHooks
from pytest_obligation.host import current_host
from pytest_obligation.integrations.task_queues import (
    TaskOutcome,
    application_admission,
    application_gate,
    keeping_signal_handlers,
    replay_safety_is_the_functions,
    settled_by_one_worker,
    the_obligation_is_the,
    worker_history,
)
from pytest_obligation.profiles.bounded_ownership import FencedOwnership
from pytest_obligation.profiles.job_retry_limits import BoundedRetry


def worker_pass(
    connection: Any,
    queues: Sequence[str],
    *,
    worker_class: Any = None,
    max_jobs: int | None = None,
    starting: bool = False,
) -> Callable[..., None]:
    """
    One burst pass of RQ's worker over ``queues``, until none has a job (or ``max_jobs`` ran).

    The worker is ``SimpleWorker`` unless ``worker_class`` names another that
    runs jobs in process. By default it is a worker already running, whose
    start-up maintenance is behind it: a death inside maintenance would hold
    RQ's maintenance lock for fifteen minutes of the server's clock, which no
    test can advance, and delays recovery rather than losing work. A
    ``starting`` worker runs that maintenance first, which is what recovery is.
    It ignores what it is given, so it can be a handoff's transition. RQ's
    signal handlers and its pubsub thread are cleaned up afterwards, even after
    a death.
    """

    def run(*_handle: object) -> None:
        from rq import Queue, SimpleWorker
        from rq.utils import now

        worker = (worker_class or SimpleWorker)(
            [Queue(name, connection=connection) for name in queues], connection=connection
        )
        if not starting:
            worker.last_cleaned_at = now()
        try:
            with keeping_signal_handlers():
                worker.work(burst=True, max_jobs=max_jobs, logging_level="WARNING")
        finally:
            thread = worker.pubsub_thread
            if thread is not None and thread.is_alive():
                thread.stop()
                thread.join(timeout=1)

    return run


def maintenance_passes(
    connection: Any,
    queues: Sequence[str],
    *,
    passes: int = 3,
    interval: timedelta = timedelta(hours=1),
    worker_class: Any = None,
) -> Callable[[], None]:
    """
    Recovery as RQ performs it: ``passes`` workers starting ``interval`` apart, each maintaining then working.

    Each runs under the host's frozen clock, ``interval`` further on, so every
    lease has expired and the intermediate list's first sighting is over a
    minute old by the next pass. Three passes let a job stranded between the
    queue and the ``StartedJobRegistry`` be seen, then judged, then run.
    """
    run = worker_pass(connection, queues, worker_class=worker_class, starting=True)

    def recover() -> None:
        frozen_clock = current_host().require("frozen_clock")
        start = datetime.now(UTC)
        for n in range(1, passes + 1):
            with frozen_clock(start + n * interval):
                run()

    return recover


def job_status(connection: Any) -> Callable[[str], str]:
    """OBSERVE: the status RQ recorded for a job, or ``missing`` once its hash is gone."""

    def status_of(job_id: str) -> str:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        try:
            status = Job.fetch(job_id, connection=connection).get_status()
            return str(getattr(status, "value", status))
        except NoSuchJobError:
            return "missing"

    return status_of


@contextmanager
def rq_callback_breaker(fail_at: int | None) -> Iterator[CountedHooks]:
    """
    A ``receiver_breaker``: count each job callback RQ runs, and fail callback ``fail_at``.

    A job's ``on_success``, ``on_failure`` and ``on_stopped`` callbacks are the
    hooks RQ runs around a job; the chosen one raises
    :class:`~pytest_obligation.worker_death.ReceiverFailed` instead of running,
    as a callback with a bug or an unreachable service would, and RQ does the
    rest as in production.
    """
    import pytest
    from rq.job import Job

    hooks = CountedHooks(fail_at, kind="job callback")
    with pytest.MonkeyPatch.context() as patch:
        for attribute in ("success_callback", "failure_callback", "stopped_callback"):
            original = getattr(Job, attribute)

            read = original.fget
            assert read is not None, f"rq.job.Job.{attribute} is no longer a readable property"

            def hooked(job: Any, read: Callable[[Any], Any] = read) -> Any:
                callback = read(job)
                return None if callback is None else hooks.counted(callback)

            patch.setattr(Job, attribute, property(hooked))
        yield hooks


def _executions(connection: Any, queue: Any, job_id: str) -> dict[str, float]:
    """The job's executions in the ``StartedJobRegistry``, with their lease deadlines."""
    members = connection.zrange(queue.started_job_registry.key, 0, -1, withscores=True)
    entries = {member.decode() if isinstance(member, bytes) else member: score for member, score in members}
    return {member: score for member, score in entries.items() if member.split(":", 1)[0] == job_id}


def _for_execution(method: Callable[..., Any], execution: Any) -> dict[str, Any]:
    """
    ``execution=`` for a worker method that takes it, else nothing.

    RQ 2.12 gives each worker one current execution and its settlements read it;
    later versions run several per worker and take the execution explicitly. A
    claim here owns its worker, so both mean the same execution.
    """
    import inspect

    return {"execution": execution} if "execution" in inspect.signature(method).parameters else {}


def ownership(connection: Any, *, enqueue: Callable[[], str], queue: str) -> FencedOwnership:
    """
    Profile B bound to RQ's own ownership: a worker's execution, its heartbeat, and ``StartedJobRegistry.cleanup``.

    ``enqueue`` enqueues one of the application's jobs on ``queue`` with a
    ``Retry``, so that reclaiming it gives it back rather than failing it. A
    claim is what a worker does before running a job: dequeue, prepare the
    execution, mark the job started. The token stands for that execution, the
    only owner identity RQ records. The fenced writes are the worker's two
    settlements, ``handle_job_success`` and ``handle_job_failure``; renewing is
    ``maintain_heartbeats``; reclaiming is ``StartedJobRegistry.cleanup``, which
    every worker's maintenance runs.
    """
    from rq import Queue, SimpleWorker
    from rq.utils import now

    rq_queue = Queue(queue, connection=connection)
    claims: dict[UUID, tuple[Any, Any, Any]] = {}

    def claim() -> tuple[str, UUID] | None:
        worker = SimpleWorker([rq_queue], connection=connection)
        worker.last_cleaned_at = now()
        worker.register_birth()
        claimed = worker.dequeue_job_and_maintain_ttl(None)
        if claimed is None:
            return None
        job, _queue = claimed
        execution = worker.prepare_execution(job)
        worker.prepare_job_execution(job, remove_from_intermediate_queue=True)
        # What perform_job records in memory before it runs the job.
        job.started_at = now()
        token = uuid5(NAMESPACE_URL, f"rq execution {execution.id}")
        claims[token] = (worker, job, execution)
        return job.id, token

    def finish(job_id: str, token: UUID) -> bool:
        # RQ's success settlement takes the execution, and checks nothing about it.
        worker, job, execution = claims[token]
        worker.handle_execution_ended(job, rq_queue, job.success_callback_timeout)
        worker.handle_job_success(
            job=job,
            queue=rq_queue,
            started_job_registry=rq_queue.started_job_registry,
            **_for_execution(worker.handle_job_success, execution),
        )
        return True

    def fail(job_id: str, token: UUID) -> bool:
        # The worker's other settlement: a failed attempt, retried while the job has retries left.
        worker, job, execution = claims[token]
        worker.handle_execution_ended(job, rq_queue, job.failure_callback_timeout)
        worker.handle_job_failure(
            job,
            rq_queue,
            started_job_registry=rq_queue.started_job_registry,
            exc_string="the attempt failed",
            **_for_execution(worker.handle_job_failure, execution),
        )
        return True

    def heartbeat(job_id: str, token: UUID) -> bool:
        # A heartbeat extends only an execution still in the registry (RQ adds with xx=True): report
        # whether this one's lease was the one renewed.
        worker, job, execution = claims[token]
        worker.maintain_heartbeats(job, **_for_execution(worker.maintain_heartbeats, execution))
        return f"{job_id}:{execution.id}" in _executions(connection, rq_queue, job_id)

    def expire(job_id: str) -> None:
        # FAULT INJECTION: the owning worker stopped heartbeating long ago.
        for member in _executions(connection, rq_queue, job_id):
            connection.zadd(rq_queue.started_job_registry.key, {member: 1}, xx=True)

    def reclaim(job_id: str) -> str | None:
        before = _executions(connection, rq_queue, job_id)
        rq_queue.started_job_registry.cleanup()
        if before and not _executions(connection, rq_queue, job_id):
            return job_status(connection)(job_id)
        return None

    def observe(job_id: str) -> Any:
        # The job's status, and its executions with their lease deadlines.
        return job_status(connection)(job_id), sorted(_executions(connection, rq_queue, job_id).items())

    return FencedOwnership(
        name=f"rq {queue} jobs",
        make_claimable=enqueue,
        claim=claim,
        fenced_write=finish,
        other_fenced_writes={"fail": fail},
        renew_lease=heartbeat,
        expire_lease=expire,
        reclaim_stalled=reclaim,
        observe=observe,
    )


def bounded_retry(
    connection: Any,
    *,
    queue: str,
    enqueue_failing: Callable[[], str],
    failures: Callable[[str], int],
    max_retries: int,
) -> BoundedRetry:
    """
    The retry safety profile bound to RQ's ``Retry``: a job that keeps failing runs ``max_retries + 1`` times.

    ``enqueue_failing`` enqueues a job whose external call keeps failing, with
    ``Retry(max=max_retries)`` and no interval; ``failures`` counts how often it
    reached that failing call. Each run is one job taken by a worker.
    """
    from rq import Queue

    rq_queue = Queue(queue, connection=connection)
    status_of = job_status(connection)
    run_one = worker_pass(connection, [queue], max_jobs=1)

    def observe(job_id: str) -> Any:
        from rq.job import Job

        return status_of(job_id), Job.fetch(job_id, connection=connection).retries_left

    return BoundedRetry(
        name=f"rq {queue} Retry(max={max_retries})",
        max_executions=max_retries + 1,
        make_failing=enqueue_failing,
        due_work=rq_queue.get_job_ids,
        run_once=run_one,
        advance_to_due=lambda _job_id: None,
        is_terminal=lambda job_id: status_of(job_id) == "failed",
        failure_attempt_count=failures,
        observe=observe,
    )


#: The worker contract's crash histories, by name: for ``handoff_gaps`` and ``findings``.
ONE_QUEUE = "the worker runs a task"
TWO_QUEUES = "a worker on two queues runs a task"

#: What RQ's worker does not promise, for any adopter's contract.
NO_SWEEP = (
    "RQ keeps no owed state to sweep: a dead worker's job is found by StartedJobRegistry.cleanup from its expired "
    "lease, which is profile B's reclaim, and is given back only when the job has retries left"
)
AT_LEAST_ONCE = (
    "a job whose worker died is failed, or run again when it has retries left; whether the dead attempt reached "
    "the service its function calls is known only to that function"
)
SERVER_CLOCK_RETENTION = (
    "a finished job's hash and result expire by Redis key TTL, on the server's clock, which no test can advance; "
    "owed jobs carry no expiry unless the application sets ttl"
)


def worker_contract(
    connection: Any,
    *,
    name: str,
    queue: str,
    enqueue: Callable[[], str],
    effect: Callable[[str], Any],
    enqueue_failing: Callable[[], str],
    failures: Callable[[str], int],
    max_retries: int,
    other_queue: str | None = None,
    external_calls: Sequence[ExternalCall] = (),
    delivery: Delivery | None = None,
    gaps: dict[Profile, dict[str, str]] | None = None,
    handoff_gaps: dict[str, str] | None = None,
    findings: Mapping[str, Findings] | None = None,
    fixtures: tuple[str, ...] = (),
) -> ObligationContract:
    """
    RQ's contract with its worker, bound to the adopter's jobs.

    ``enqueue`` enqueues one of the adopter's jobs on ``queue``, with a
    ``Retry`` and whatever callbacks it uses, and returns its id; ``effect``
    observes what the job did. Ownership (profile B) and bounded retry are
    claimed and proven against RQ itself. The worker's crash histories run the
    job with a worker on ``queue``, and, when ``other_queue`` is given, with a
    worker listening on both, which RQ dequeues differently. ``gaps`` (by
    profile or safety profile) and ``handoff_gaps`` declare what the adopter
    found, each a strict xfail; ``findings`` pins what each history leaves, by
    history name (:data:`ONE_QUEUE`, :data:`TWO_QUEUES`), checked in the same
    run as its verdict. ``fixtures`` are requested by every generated
    case, for example one that empties the Redis database.
    """
    findings = findings or {}
    run_worker = worker_pass(connection, [queue])
    status_of = job_status(connection)
    histories: list[HandoffHistory[str, TaskOutcome]] = [
        worker_history(
            enqueue=enqueue,
            effect=effect,
            status_of=status_of,
            run_worker=run_worker,
            external_calls=external_calls,
            name=ONE_QUEUE,
            findings=findings.get(ONE_QUEUE),
        )
    ]
    if other_queue is not None:
        histories.append(
            worker_history(
                enqueue=enqueue,
                effect=effect,
                status_of=status_of,
                run_worker=worker_pass(connection, [other_queue, queue]),
                external_calls=external_calls,
                name=TWO_QUEUES,
                findings=findings.get(TWO_QUEUES),
            )
        )
    recovery_queues = [queue] if other_queue is None else [other_queue, queue]
    gaps = gaps or {}
    return ObligationContract(
        name=name,
        adoption=Adoption.LEGACY,
        transactional=True,
        profiles={
            Profile.A: Decline(NO_SWEEP),
            Profile.B: Claim(gaps=gaps.get(Profile.B, {})),
            Profile.C: Decline(AT_LEAST_ONCE),
            Profile.D: Decline(SERVER_CLOCK_RETENTION),
            Profile.E: settled_by_one_worker("job"),
            Profile.F: the_obligation_is_the("job"),
            Profile.H: replay_safety_is_the_functions(
                "job", "RQ", runs_again="a retry, or a reclaim after a worker's death"
            ),
            Profile.J: Claim(gaps=gaps.get(Profile.J, {})),
            Profile.G: application_gate("RQ"),
            Profile.I: application_admission("RQ"),
        },
        retry=lambda: bounded_retry(
            connection, queue=queue, enqueue_failing=enqueue_failing, failures=failures, max_retries=max_retries
        ),
        ownership=lambda: ownership(connection, enqueue=enqueue, queue=queue),
        handoffs=tuple(histories),
        handoff_delivery=delivery
        or CallableDelivery(name=f"{name}: rq worker", recover=maintenance_passes(connection, recovery_queues)),
        handoff_gaps=handoff_gaps or {},
        fixtures=fixtures,
    )


__all__ = [
    "AT_LEAST_ONCE",
    "ONE_QUEUE",
    "TWO_QUEUES",
    "NO_SWEEP",
    "SERVER_CLOCK_RETENTION",
    "bounded_retry",
    "job_status",
    "maintenance_passes",
    "ownership",
    "rq_callback_breaker",
    "worker_contract",
    "worker_pass",
]
