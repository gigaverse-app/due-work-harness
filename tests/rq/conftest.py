from collections.abc import Iterator

import pytest

from due_work_harness import configure
from due_work_harness.integrations.redis import redis_host
from due_work_harness.integrations.rq import rq_callback_breaker
from tests.rq.connection import CONNECTION

configure(redis_host(CONNECTION, {"rq"}, receiver_breaker=rq_callback_breaker))


@pytest.fixture
def empty_redis() -> Iterator[None]:
    CONNECTION.flushdb()
    yield
    CONNECTION.flushdb()
