import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from prefect.deployments.runner import RunnerDeployment

from due_work_harness.binding import assert_binding_reaches_production
from due_work_harness.host import Host, hosted
from due_work_harness.integrations.prefect import assert_prefect_recurs, prefect_flow_call
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
