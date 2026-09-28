"""
The Celery worker integration, run against Celery itself on the reference task.

The worker is a real ``celery worker`` with one prefork child, as a child
process. What Celery does not hold is one gap, a strict xfail, pinned history by
history in ``FINDINGS`` within the same run, so a change in Celery turns this
suite red until the table is updated.
"""

from typing import cast

import redis
from celery.result import AsyncResult

from due_work_harness import Findings, due_work_contract_suite
from due_work_harness.integrations.celery_worker import worker_contract, worker_history
from tests.celery_worker import app as reference

MESSAGE = "your order has shipped"
SENT = f"sent {MESSAGE!r}"


def send() -> str:
    for url in (reference.BROKER, reference.BACKEND):
        redis.Redis.from_url(url).flushdb()
    reference.RECORDS.flushdb()
    return reference.send_message.apply_async(
        (MESSAGE,), link=reference.announce.s(), link_error=reference.report_failure.s()
    ).id


def what_happened(task_id: str) -> tuple[str, int, tuple[str, ...]]:
    # decode_responses=True makes every reply a str; redis-py's annotations do not follow it.
    announced = cast("list[str]", reference.RECORDS.lrange("announced", 0, -1))
    return (
        str(AsyncResult(task_id, app=reference.app).state),
        reference.RECORDS.llen("sent"),
        # Sorted: the link and the error callback run in different processes, in either order.
        tuple(sorted(announced)),
    )


# What each failure leaves after the worker starts again; every history not listed reaches normal operation.
FINDINGS = Findings(
    ("SUCCESS", 1, (SENT,)),
    {
        "died at task_prerun": ("FAILURE", 0, ("failed: WorkerLostError",)),
        "died at mark_as_done": ("FAILURE", 1, ("failed: WorkerLostError", SENT)),
        "died at task_postrun": ("SUCCESS", 1, ("failed: WorkerLostError", SENT)),
        "the task's on_success hook raised": ("SUCCESS", 1, ("failed: ReceiverFailed", SENT)),
        "the broker refused the task's link": ("FAILURE", 1, ("failed: OperationalError",)),
    },
)

SEND_MESSAGE = worker_history(
    name="the reference worker runs send_message",
    app="tests.celery_worker.app:app",
    task=reference.send_message.name,
    send=send,
    observe=what_happened,
    initial=("PENDING", 0, ()),
    settled=lambda task_id: AsyncResult(task_id, app=reference.app).ready(),
    findings=FINDINGS,
)

CONTRACT = worker_contract(
    name="celery reference worker",
    history=SEND_MESSAGE,
    gap="a failure after the result is stored or the link published still runs the failure path",
)


@due_work_contract_suite(CONTRACT)
class TestCeleryWorkerContract:
    """Generated from CONTRACT: the process case checks FINDINGS and the verdict in one run."""
