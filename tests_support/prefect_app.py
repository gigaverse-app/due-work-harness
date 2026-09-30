"""Production-shaped Prefect flows used by the integration's controls."""

import asyncio

from prefect import flow


@flow
async def record(values: list[int], value: int) -> int:
    await asyncio.sleep(0)
    values.append(value)
    return value


@flow
def other() -> None:
    pass
