"""Bind Prefect flow bodies and inspect Prefect's own recurring schedule calculation.

Application histories call .fn on a caller-owned asyncio.Runner: the entire
coroutine finishes before a history observes it, and clients retain one loop.
This does not simulate the Prefect engine's task retries, hooks, or deployment
delivery. Use a real engine/process history for those claims. Schedule evidence
checks the supplied deployment declaration, not that a remote worker is running.
"""

import asyncio
import inspect
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import wraps
from typing import Any

from due_work_harness.models import DueWorkContractDesignError


def prefect_flow_call(flow: Any, runner: asyncio.Runner) -> Callable[..., Any]:
    """A synchronous production binding that completes a real Prefect flow's body."""
    from prefect import Flow

    if not isinstance(flow, Flow):
        raise DueWorkContractDesignError("prefect_flow_call requires a Prefect Flow")

    @wraps(flow.fn)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        result = flow.fn(*args, **kwargs)
        if inspect.iscoroutine(result):
            return runner.run(result)
        if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
            raise DueWorkContractDesignError("flow body returned deferred work; bind a coroutine that awaits it")
        return result

    return invoke


def assert_prefect_recurs(
    deployment: Any, flow: Any, *, start: datetime, within: timedelta, runner: asyncio.Runner
) -> None:
    """The declaration names this flow and supplies three recurring due times within its bound."""
    from prefect.deployments.runner import RunnerDeployment
    from prefect.server.schemas.schedules import SCHEDULE_TYPES
    from pydantic import TypeAdapter

    if not isinstance(deployment, RunnerDeployment):
        raise DueWorkContractDesignError("schedule evidence requires a Prefect RunnerDeployment")
    expected = RunnerDeployment.from_flow(flow, name=deployment.name)
    assert deployment.flow_name == expected.flow_name and deployment.entrypoint == expected.entrypoint, (
        "deployment does not point at the recovery flow"
    )
    assert not deployment.paused, "the recovery deployment is paused"
    assert within > timedelta(0), "recurrence bound must be positive"
    assert deployment.schedules, "the recovery deployment has no schedules"
    active = [entry for entry in deployment.schedules if entry.active]
    assert active, "the recovery deployment has no active schedule"
    dates: set[datetime] = set()
    for entry in active:
        assert entry.schedule is not None, "active schedule has no recurrence rule"
        schedule = TypeAdapter(SCHEDULE_TYPES).validate_python(entry.schedule.model_dump())
        dates.update(runner.run(schedule.get_dates(n=3, start=start, end=start + within * 3)))
    following = sorted(date for date in dates if date > start)
    assert len(following) >= 2, "the recovery schedule does not recur within its bound"
    previous = start
    for date in following:
        assert date - previous <= within, "the recovery schedule exceeds its declared interval"
        previous = date
