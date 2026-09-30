"""Generated application guarantees through RQ's real Redis queue and SimpleWorker."""

from collections.abc import Callable

from rq import Queue
from rq.job import JobStatus
from test_bindings import catalog_contract

from due_work_harness import due_work_contract_suite
from due_work_harness.integrations.rq import worker_pass
from tests.rq.connection import CONNECTION
from tests_support.catalog_executor import execute, registered


def rq_run(operation: Callable[[], None]) -> None:
    with registered(operation) as identity:
        job = Queue("catalog", connection=CONNECTION).enqueue(execute, identity)
        worker_pass(CONNECTION, ["catalog"])()
        assert job.get_status(refresh=True) == JobStatus.FINISHED, job.exc_info
        # The empty_redis fixture owns cleanup after the proof, not this worker path.


CONTRACT = catalog_contract("catalog on RQ", rq_run).model_copy(update={"fixtures": ("empty_redis",)})


@due_work_contract_suite(CONTRACT)
class TestCatalogOnRQ:
    pass
