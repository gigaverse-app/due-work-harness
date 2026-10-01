import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from pytest_obligation import configure
from pytest_obligation.integrations.redis import redis_host
from pytest_obligation.integrations.rq import rq_callback_breaker
from tests.rq.connection import CONNECTION

configure(redis_host(CONNECTION, {"rq"}, receiver_breaker=rq_callback_breaker))


@pytest.fixture
def empty_redis() -> Iterator[None]:
    CONNECTION.flushdb()
    yield
    CONNECTION.flushdb()


#: The cases that reclaim a job with a failure callback. RQ does not support Windows, and there the reclaim
#: fails inside RQ: StartedJobRegistry.cleanup runs the callback under UnixSignalDeathPenalty, which arms
#: SIGALRM, and Windows has no SIGALRM. Every other case runs on Windows too.
RECLAIMING_CASES = (
    "B-assert_fence_rotates_through_recovery",
    "B-assert_expired_lease_permits_reclaim",
    "B-assert_lease_renewal_is_fenced",
    "handoff-the worker runs a task-assert_crash_at_every_commit_converges",
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(
        reason="RQ does not support Windows: StartedJobRegistry.cleanup runs a reclaimed job's failure callback "
        "under UnixSignalDeathPenalty, which arms SIGALRM, and Windows has no SIGALRM"
    )
    # The hook sees the whole session's items; only this directory's contract is RQ's.
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents and any(f"[{case}]" in item.nodeid for case in RECLAIMING_CASES):
            item.add_marker(skip)
