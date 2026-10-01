import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from prefect import flow
from prefect.deployments.runner import RunnerDeployment

from pytest_obligation.binding import assert_binding_reaches_production
from pytest_obligation.host import Host, hosted
from pytest_obligation.integrations.prefect import assert_prefect_recurs, prefect_flow_call
from pytest_obligation.models import ObligationContractDesignError
from tests_support.prefect_app import other, record


def test_flow_binding_finishes_coroutines_and_keeps_production_identity():
    values = []
    with asyncio.Runner() as runner, hosted(Host(production_packages=frozenset({"tests_support.prefect_app"}))):
        call = prefect_flow_call(record, runner)
        assert call(values, 7) == 7
        assert call(values, 9) == 9
        assert values == [7, 9]
        assert_binding_reaches_production(
            adopter="prefect", field="flow", binding=call, forbidden=(), production_shape="real flow"
        )


@pytest.mark.parametrize("fault", [None, "paused", "inactive", "missing", "slow", "wrong_flow", "wrong_entrypoint"])
def test_schedule_evidence_uses_prefects_deployment_and_date_rules(fault):
    deployment = RunnerDeployment.from_flow(record, name="recovery", cron="0 * * * *")
    assert deployment.schedules
    if fault == "paused":
        deployment.paused = True
    elif fault == "inactive":
        deployment.schedules[0].active = False
    elif fault == "missing":
        deployment.schedules = []
    elif fault == "slow":
        deployment = RunnerDeployment.from_flow(record, name="recovery", cron="0 0 * * *")
    elif fault == "wrong_flow":
        deployment = RunnerDeployment.from_flow(other, name="recovery", cron="0 * * * *")
    elif fault == "wrong_entrypoint":
        deployment.entrypoint = "missing.py:record"
    with asyncio.Runner() as runner:

        def check():
            assert_prefect_recurs(
                deployment,
                record,
                start=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
                within=timedelta(hours=1),
                runner=runner,
            )

        if fault is None:
            check()
        else:
            with pytest.raises(AssertionError):
                check()


@pytest.mark.parametrize("async_body", [False, True])
def test_binding_refuses_deferred_results_from_sync_and_async_bodies(async_body):
    def deferred():
        yield "unobserved work"

    @flow
    def sync_flow():
        return deferred()

    @flow
    async def async_flow():
        return deferred()

    with asyncio.Runner() as runner:
        with pytest.raises(ObligationContractDesignError, match="deferred work"):
            prefect_flow_call(async_flow if async_body else sync_flow, runner)()


def test_recurrence_checks_three_future_occurrences_even_at_an_exact_tick():
    # Only two future runs cannot establish the claimed three-run recurrence.
    deployment = RunnerDeployment.from_flow(
        record, name="finite", rrule="DTSTART:20260901T000000Z\nRRULE:FREQ=HOURLY;COUNT=3"
    )
    with asyncio.Runner() as runner, pytest.raises(AssertionError, match="recur"):
        assert_prefect_recurs(
            deployment,
            record,
            start=datetime(2026, 9, 1, tzinfo=UTC),
            within=timedelta(hours=1),
            runner=runner,
        )


def test_hourly_schedule_at_exact_tick_still_has_three_future_runs():
    deployment = RunnerDeployment.from_flow(record, name="hourly", cron="0 * * * *")
    with asyncio.Runner() as runner:
        assert_prefect_recurs(
            deployment,
            record,
            start=datetime(2026, 9, 1, tzinfo=UTC),
            within=timedelta(hours=1),
            runner=runner,
        )


@pytest.mark.parametrize("await_child", [False, True])
def test_rejected_task_cannot_resume_during_a_later_history(await_child):
    effects = []
    release = asyncio.Event()

    async def effect():
        await release.wait()
        effects.append("written")
        return "done"

    @flow
    async def application():
        task = asyncio.create_task(effect())
        if await_child:
            release.set()
            return await task
        return task

    async def next_history():
        release.set()
        await asyncio.sleep(0)

    with asyncio.Runner() as runner:
        call = prefect_flow_call(application, runner)
        if await_child:
            assert call() == "done"
        else:
            with pytest.raises(ObligationContractDesignError, match="deferred work"):
                call()
        runner.run(next_history())
        assert effects == (["written"] if await_child else [])


def test_rejected_task_is_cancelled_before_the_runner_is_resumed():
    effects = []

    async def ready_effect():
        await asyncio.sleep(0)
        effects.append("written")

    @flow
    async def application():
        return asyncio.create_task(ready_effect())

    with asyncio.Runner() as runner:
        with pytest.raises(ObligationContractDesignError, match="deferred work"):
            prefect_flow_call(application, runner)()
        assert effects == []
