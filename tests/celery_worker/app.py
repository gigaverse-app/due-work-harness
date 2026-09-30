"""The reference Celery application the worker self-tests run: a task that sends a message, its link, its errback."""

from typing import Any

import redis
from celery import Celery

from tests.redis_databases import redis_url

BROKER = redis_url("celery broker")
BACKEND = redis_url("celery backend")
#: What the tasks did, readable from the test process and every worker process.
RECORDS = redis.Redis.from_url(redis_url("celery records"), decode_responses=True)

app = Celery("due_work_reference", broker=BROKER, backend=BACKEND)
app.conf.update(task_acks_late=True, worker_prefetch_multiplier=1)


@app.task
def send_message(message: str) -> str:
    # EXTERNAL SEAM: the message leaves for the customer.
    RECORDS.rpush("sent", message)
    return message


@app.task
def announce(result: str) -> None:
    RECORDS.rpush("announced", f"sent {result!r}")


@app.task
def report_failure(request: Any, exc: BaseException, traceback: Any) -> None:
    RECORDS.rpush("announced", f"failed: {type(exc).__name__}")
