"""Generated application histories through Prefect's flow engine and local API server."""

import asyncio
from collections.abc import Callable, Iterator

import pytest
from prefect import flow
from prefect.testing.utilities import prefect_test_harness
from test_bindings import catalog_contract

from pytest_obligation import due_work_contract_suite
from tests_support.catalog_executor import execute, registered


@flow(log_prints=False, persist_result=False)
async def catalog_turn(identity: str) -> None:
    """A real flow invocation carries a stable operation identity through the engine."""
    await asyncio.sleep(0)
    execute(identity)


@pytest.fixture(scope="module")
def catalog_prefect_server() -> Iterator[None]:
    """Use Prefect's supported isolated API/database for engine semantics."""
    with prefect_test_harness():
        yield


def prefect_run(operation: Callable[[], None]) -> None:
    with registered(operation) as identity, asyncio.Runner() as runner:
        # Calling the decorated flow invokes the engine, including API state transitions.
        runner.run(catalog_turn(identity))


CONTRACT = catalog_contract("catalog through Prefect engine", prefect_run).model_copy(
    update={"fixtures": ("catalog_prefect_server",)}
)


@due_work_contract_suite(CONTRACT)
class TestCatalogThroughPrefect:
    pass
