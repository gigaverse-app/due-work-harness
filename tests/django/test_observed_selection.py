"""
Observing a production tick instead of restating it: the query it evaluates, and the messages it would send.

``selection_built_by`` returns the tick's own query, unevaluated and current;
``held_publications`` records each Celery message with its arguments and runs
none of them, even under eager execution.
"""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from celery import Celery
from django.utils import timezone

from pytest_obligation.integrations.celery import Publication, held_publications
from pytest_obligation.integrations.django import lifecycle_references as ref
from pytest_obligation.integrations.django.selection import selection_built_by

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def attempt_table() -> Iterator[None]:
    with ref.lifecycle_attempt_table():
        yield
    ref.OUTBOX.clear()


def _attempt(age: timedelta) -> int:
    return ref.LifecycleAttempt.objects.create(status=ref.Status.REQUESTED, updated_at=timezone.now() - age).pk


def test_the_selection_a_tick_evaluates_is_observed_live() -> None:
    stale = _attempt(timedelta(hours=1))
    _attempt(timedelta(0))
    observed = selection_built_by(ref.run_recovery_tick, ref.LifecycleAttempt)
    assert observed.query.order_by == ref.due_for_recovery().query.order_by, "the tick's own query, ordering included"
    assert [attempt.pk for attempt in observed] == [stale]
    # Each observation runs the tick again: a row that becomes due is in the next one, as in the next tick.
    older = _attempt(timedelta(hours=2))
    assert [attempt.pk for attempt in selection_built_by(ref.run_recovery_tick, ref.LifecycleAttempt)] == [older, stale]


def test_a_tick_that_evaluates_no_query_of_the_model_is_refused() -> None:
    with pytest.raises(AssertionError, match="evaluated no LifecycleAttempt query"):
        selection_built_by(lambda: None, ref.LifecycleAttempt)


@pytest.fixture
def eager_task() -> Iterator[object]:
    app = Celery("self-test", set_as_current=False)
    app.conf.task_always_eager = True
    runs: list[int] = []

    @app.task(name="self-test.work")
    def work(order_id: int) -> None:
        runs.append(order_id)

    work.runs = runs  # type: ignore[attr-defined]
    yield work


def test_held_publications_record_the_message_and_run_nothing(eager_task) -> None:  # noqa: ANN001
    with held_publications() as held:
        eager_task.delay(7)
        eager_task.apply_async(args=(8,), kwargs={"urgent": True}, countdown=5)
    assert held == [Publication("self-test.work", (7,), {}), Publication("self-test.work", (8,), {"urgent": True})]
    assert eager_task.runs == []
    eager_task.delay(9)
    assert eager_task.runs == [9], "outside the block, an eager publication runs again"
