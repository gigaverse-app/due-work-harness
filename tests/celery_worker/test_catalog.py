"""Application proofs through Celery's eager task tracer; process deaths have their own suite."""

from collections.abc import Callable

from celery import Celery
from test_bindings import catalog_contract

from due_work_harness import due_work_contract_suite
from tests_support.catalog_executor import execute, registered

APP = Celery("catalog-proof", broker="memory://", backend="cache+memory://")
TASK = APP.task(execute)


def celery_run(operation: Callable[[], None]) -> None:
    with registered(operation) as identity:
        # Run Celery's task tracer and propagate failures; apply_async/eager settings
        # must not silently enqueue work that this deterministic session never sees.
        TASK.apply((identity,), throw=True).get(propagate=True)


CONTRACT = catalog_contract("catalog through Celery", celery_run)


@due_work_contract_suite(CONTRACT)
class TestCatalogThroughCelery:
    pass
