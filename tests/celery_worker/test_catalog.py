"""Application guarantees through Redis delivery and a real Celery solo worker.

Keeping the worker in a thread lets the external fault controller release accepted
requests deterministically. The separate prefork suite retains process-death proofs.
"""

from collections.abc import Callable, Iterator

import pytest
import redis
from celery import Celery
from celery.contrib.testing.worker import start_worker
from test_bindings import catalog_contract

from pytest_obligation import due_work_contract_suite
from tests.redis_databases import redis_url
from tests_support.catalog_executor import execute, registered

APP = Celery("catalog-proof", broker=redis_url("celery broker"), backend=redis_url("celery backend"))
APP.conf.update(task_default_queue="catalog", worker_prefetch_multiplier=1, task_acks_late=True)
TASK = APP.task(name="catalog.execute")(execute)


@pytest.fixture(scope="module")
def catalog_worker() -> Iterator[None]:
    """One real worker per process; xdist owns separate Redis databases."""
    for role in ("celery broker", "celery backend"):
        redis.Redis.from_url(redis_url(role)).flushdb()
    with start_worker(APP, pool="solo", queues=("catalog",), perform_ping_check=False, shutdown_timeout=15):
        yield
    for role in ("celery broker", "celery backend"):
        redis.Redis.from_url(redis_url(role)).flushdb()


def celery_run(operation: Callable[[], None]) -> None:
    with registered(operation) as identity:
        result = TASK.apply_async((identity,))
        # Await completion before releasing the application or its external fault controller.
        result.get(timeout=15, propagate=True, disable_sync_subtasks=False)
        result.forget()


CONTRACT = catalog_contract("catalog through Celery", celery_run).model_copy(update={"fixtures": ("catalog_worker",)})


@due_work_contract_suite(CONTRACT)
class TestCatalogThroughCelery:
    pass
