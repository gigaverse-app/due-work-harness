"""
The Celery publication breaker counts messages, not the calls that make one.

A non-eager ``apply_async`` publishes through ``send_task``; counting both would
make "refuse publication 2" refuse the inner half of message 1. An eager task
runs in place, and what it publishes is a message of its own.
"""

from collections.abc import Iterator

import pytest
from celery import Celery
from kombu.exceptions import OperationalError

from due_work_harness.integrations.celery import celery_publication_breaker


@pytest.fixture
def broker_app() -> Iterator[Celery]:
    app = Celery("self-test-broker", broker="memory://", set_as_current=False)

    @app.task(name="self-test.notify")
    def notify(order_id: int) -> None:
        pass

    yield app


@pytest.fixture
def eager_app() -> Iterator[Celery]:
    app = Celery("self-test-eager", set_as_current=False)
    app.conf.task_always_eager = True

    @app.task(name="self-test.follow-up")
    def follow_up(order_id: int) -> None:
        pass

    @app.task(name="self-test.place")
    def place(order_id: int) -> None:
        follow_up.delay(order_id)

    yield app


def test_a_non_eager_delay_is_one_publication(broker_app: Celery) -> None:
    notify = broker_app.tasks["self-test.notify"]
    with celery_publication_breaker(None) as publications:
        notify.delay(1)
        notify.delay(2)
    assert publications.count == 2


def test_refusing_a_publication_refuses_that_message(broker_app: Celery) -> None:
    notify = broker_app.tasks["self-test.notify"]
    with celery_publication_breaker(2) as publications:
        notify.delay(1)
        with pytest.raises(OperationalError):
            notify.delay(2)
    assert publications.count == 2
    assert publications.failure is not None


def test_an_eager_tasks_own_publications_are_messages_of_their_own(eager_app: Celery) -> None:
    with celery_publication_breaker(None) as publications:
        eager_app.tasks["self-test.place"].delay(1)
    assert publications.count == 2
