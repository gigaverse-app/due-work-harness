"""Generated histories bind an actual async Prefect flow body, with complete coroutine lifetimes."""

import asyncio
from collections.abc import Callable

from contract_bindings import catalog_contract
from prefect import flow

from due_work_harness import due_work_contract_suite
from due_work_harness.integrations.prefect import prefect_flow_call


@flow
async def catalog_turn(operation: Callable[[], None]) -> None:
    await asyncio.sleep(0)
    operation()


def prefect_run(operation: Callable[[], None]) -> None:
    with asyncio.Runner() as runner:
        prefect_flow_call(catalog_turn, runner)(operation)


CONTRACT = catalog_contract("catalog through Prefect flow body", prefect_run)


@due_work_contract_suite(CONTRACT)
class TestCatalogThroughPrefect:
    pass
