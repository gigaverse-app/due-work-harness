"""
The RQ integration's worker contract, run against RQ itself on the reference jobs.

What RQ does not hold is declared as gaps, each a strict xfail: a fix in RQ
fails this suite until the declaration changes, so the integration's claims
stay true of the RQ version the lock pins. ``FINDINGS`` pins each finding
history by history, in the same run as the verdict.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from functools import partial

from rq import Callback, Queue, Retry
from rq.job import Job

from due_work_harness import Claim, ExecutionGate, Profile, ReplaySafeEffect, due_work_contract_suite
from due_work_harness.crash_histories import ExternalCall, Findings
from due_work_harness.integrations.redis import redis_key_writes
from due_work_harness.integrations.rq import ONE_QUEUE, worker_contract, worker_pass
from due_work_harness.integrations.task_queues import TaskOutcome
from tests.rq import jobs
from tests.rq.connection import CONNECTION

QUEUE = "harness"
MESSAGE = "your order has shipped"


def a_message_owed() -> str:
    # ARRANGE: a job that sends one message and announces it, with one retry.
    jobs.outbox.clear()
    return (
        Queue(QUEUE, connection=CONNECTION)
        .enqueue(
            jobs.send_message,
            MESSAGE,
            retry=Retry(max=1),
            on_success=Callback(jobs.announce_success),
            on_failure=Callback(jobs.announce_failure),
        )
        .id
    )


def what_happened(_job_id: str) -> tuple[int, tuple[str, ...]]:
    # OBSERVE: how many times the message left, and what the callbacks announced.
    return jobs.outbox.sent[MESSAGE], tuple(jobs.outbox.announced)


def a_failing_job() -> str:
    jobs.outbox.clear()
    return Queue(QUEUE, connection=CONNECTION).enqueue(jobs.call_down_service, retry=Retry(max=2)).id


SENT = f"sent {MESSAGE!r}"


def _job(sent: int, *announced: str, status: str = "finished") -> TaskOutcome:
    return TaskOutcome(status=status, effect=(sent, announced))


# What each history leaves after RQ's recovery; every history not listed reaches normal operation.
# Commit numbers are RQ's SimpleWorker's Redis writes for one job, as RQ 2.12.0 (the lock) makes
# them. On RQ main the two FAILED entries do not happen and the numbers move.
FINDINGS = Findings(
    _job(1, SENT),
    {
        "worker died after commit 10": _job(1, "failed: AbandonedJobError", SENT, status="failed"),
        "worker died after commit 11": _job(1, "failed: AbandonedJobError", SENT),
        "worker died after commit 12": _job(1, "failed: AbandonedJobError", SENT),
        "worker died after commit 13": _job(2, "failed: AbandonedJobError", SENT),
        "worker died after external call 1": _job(2, "failed: AbandonedJobError", SENT),
        "the reply to commit 10 was lost": _job(1, "failed: AbandonedJobError", SENT, status="failed"),
        "the reply to commit 11 was lost": _job(1, "failed: ConnectionError", SENT),
        "the reply to commit 12 was lost": _job(1, "failed: ConnectionError", SENT),
        "the reply to commit 13 was lost": _job(2, "failed: ConnectionError", SENT),
        "the reply to commit 14 was lost": _job(2, SENT, "failed: ConnectionError", SENT),
        "signal receiver 1 failed": _job(2, "failed: ReceiverFailed", SENT),
    },
)


def message_replay() -> ReplaySafeEffect:
    # ARRANGE: a real RQ job carrying one message identity.
    # REAL PRODUCTION: Job.perform invokes the reference application's actual function twice.
    # EXTERNAL SEAM: Outbox.send records every customer message.
    # OBSERVE: exact message count, so two deliveries cannot be declared equivalent.
    jobs.outbox.clear()
    return ReplaySafeEffect(
        name="RQ message task",
        prepare=lambda: Queue(QUEUE, connection=CONNECTION).enqueue(jobs.send_message, MESSAGE),
        execute=Job.perform,
        observe=lambda job: jobs.outbox.sent[job.args[0]],
        execution_count_for=lambda job: jobs.outbox.sent[job.args[0]],
    )


@contextmanager
def dependent_message_gate(prerequisites: int) -> Iterator[ExecutionGate]:
    # ARRANGE: a real deferred RQ message depending on one or two unfinished jobs on another queue.
    # REAL PRODUCTION: RQ dependency admission, completion/enqueue_dependents, and SimpleWorker polling.
    # EXTERNAL SEAM: the actual message outbox; no notification is delivered to the dependent worker.
    # OBSERVE: persisted reservation, exact-key Redis writes and customer messages independently.
    jobs.outbox.clear()
    parents = Queue("prerequisites", connection=CONNECTION)
    queue = Queue(QUEUE, connection=CONNECTION)
    dependencies = [parents.enqueue(jobs.send_message, f"prerequisite-{index}") for index in range(prerequisites)]
    job = queue.enqueue(jobs.send_message, MESSAGE, depends_on=dependencies)

    def reserved() -> tuple[bytes | str | None, ...]:
        fields = CONNECTION.hmget(job.key, "data", "timeout", "retries_left")
        return tuple(fields)

    with redis_key_writes(CONNECTION, job.key) as writes:
        yield ExecutionGate(
            name=f"RQ message with {prerequisites} prerequisites",
            identity=job.id,
            due_work=queue.get_job_ids,
            owed_work=lambda: [*queue.get_job_ids(), *queue.deferred_job_registry.get_job_ids()],
            reserved_state=reserved,
            routes={"dependent worker": worker_pass(CONNECTION, [QUEUE])},
            make_eligible=worker_pass(CONNECTION, ["prerequisites"]),
            recover=worker_pass(CONNECTION, [QUEUE]),
            advance=lambda _elapsed: None,
            executions=lambda: jobs.outbox.sent[MESSAGE],
            mutation_count=lambda: writes.commits,
            observe=lambda: jobs.outbox.sent[MESSAGE],
            expected=1,
            effect_calls=lambda: jobs.outbox.sent[MESSAGE],
            recovery_interval=timedelta(seconds=1),
            recovery_timeout=timedelta(seconds=3),
        )


CONTRACT = worker_contract(
    CONNECTION,
    name="rq reference jobs",
    queue=QUEUE,
    enqueue=a_message_owed,
    effect=what_happened,
    enqueue_failing=a_failing_job,
    failures=lambda _job_id: jobs.outbox.failed_calls,
    max_retries=2,
    external_calls=(ExternalCall(jobs.Outbox, "send"),),
    gaps={Profile.B: {"assert_stale_token_is_rejected": "RQ settles a job without checking its execution"}},
    handoff_gaps={ONE_QUEUE: "a lost reply or a raising on_success runs a finished job again"},
    findings={ONE_QUEUE: FINDINGS},
    fixtures=("empty_redis",),
)


# Task semantics belong to this adopter. The generic worker contract deliberately
# declines H until an application supplies an observation of its external effect.
CONTRACT = CONTRACT.model_copy(
    update={
        "profiles": {
            **CONTRACT.profiles,
            Profile.G: Claim(),
            Profile.H: Claim(
                gaps={"assert_replay_converges": "The reference task sends a second customer message on blind replay."}
            ),
        },
        "replay": message_replay,
        "eligibility": {str(count): partial(dependent_message_gate, count) for count in (1, 2)},
    }
)


@due_work_contract_suite(CONTRACT)
class TestRQWorkerContract:
    """Generated from CONTRACT: the handoff case checks FINDINGS and the verdict in one run."""
