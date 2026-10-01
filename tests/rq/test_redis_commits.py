"""
The Redis worker killer and reply breaker see exactly the writes a worker commits.

A pipeline travels in one round trip, so it is one commit when it writes; a
read commits nothing; a standalone write and a script are one commit each.
After a death nothing more reaches the server; after a lost reply the write
has landed and the worker lives on.
"""

import pytest
from redis import ConnectionPool, Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from pytest_obligation.integrations.redis import redis_key_writes, redis_reply_breaker, redis_worker_killer
from pytest_obligation.worker_death import WorkerDied
from tests.rq.connection import CONNECTION

pytestmark = pytest.mark.usefixtures("empty_redis")


def _writes() -> None:
    CONNECTION.get("a")  # a read: no commit
    CONNECTION.set("a", 1)  # commit 1
    with CONNECTION.pipeline() as pipeline:  # commit 2: one round trip
        pipeline.set("b", 2)
        pipeline.expire("b", 60)
        pipeline.execute()
    with CONNECTION.pipeline() as pipeline:  # a pipeline of reads: no commit
        pipeline.get("a")
        pipeline.get("b")
        pipeline.execute()
    CONNECTION.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "c", 3)  # commit 3: a script


def test_the_killer_counts_each_write_round_trip() -> None:
    with redis_worker_killer(CONNECTION)(None) as worker:
        _writes()
    assert worker.commits == 3
    assert not worker.dead


@pytest.mark.parametrize("commit", [1, 2, 3])
def test_nothing_reaches_the_server_after_a_death(commit: int) -> None:
    with pytest.raises(WorkerDied, match=f"after commit {commit}"):
        with redis_worker_killer(CONNECTION)(commit):
            _writes()
    written = [key for key in ("a", "b", "c") if CONNECTION.exists(key)]
    assert written == ["a", "b", "c"][:commit]


def test_a_dead_worker_sends_nothing_more() -> None:
    with redis_worker_killer(CONNECTION)(1) as worker:
        with pytest.raises(WorkerDied):
            CONNECTION.set("a", 1)
        with pytest.raises(WorkerDied, match="the worker is dead"):
            CONNECTION.get("a")
    assert worker.dead
    # Outside the history the client is the application's again.
    assert CONNECTION.get("a") == b"1"


def test_a_client_on_another_connection_pool_is_not_watched() -> None:
    # A task's own client, for example: its writes are not the worker's commits.
    other = Redis(connection_pool=ConnectionPool(**CONNECTION.connection_pool.connection_kwargs))
    with redis_worker_killer(CONNECTION)(1) as worker:
        other.set("x", 1)
    assert worker.commits == 0
    assert not worker.dead


@pytest.mark.parametrize("commit", [1, 2, 3])
def test_a_lost_reply_lands_the_write_and_the_worker_lives_on(commit: int) -> None:
    with redis_reply_breaker(CONNECTION)(commit) as replies:
        with pytest.raises(RedisConnectionError, match=f"the reply to commit {commit} was lost"):
            _writes()
        # The process is alive: it can go on writing.
        CONNECTION.set("after", 1)
    assert replies.failure is not None
    assert [key for key in ("a", "b", "c") if CONNECTION.exists(key)] == ["a", "b", "c"][:commit]
    assert CONNECTION.get("after") == b"1"


def test_a_killer_and_a_reply_breaker_count_the_same_commits() -> None:
    with redis_worker_killer(CONNECTION)(None) as worker, redis_reply_breaker(CONNECTION)(None) as replies:
        _writes()
    assert worker.commits == replies.count == 3


def test_key_write_observation_sees_same_value_writes_and_pipelines() -> None:
    with redis_key_writes(CONNECTION, "target") as counter:
        CONNECTION.set("unrelated", "value")
        CONNECTION.get("target")
        assert counter.commits == 0
        CONNECTION.set("target", "value")
        CONNECTION.set("target", "value")
        with CONNECTION.pipeline() as pipeline:
            pipeline.set("target", "value")
            pipeline.set("unrelated", "value")
            pipeline.execute()
        assert counter.commits == 3
        with CONNECTION.pipeline() as pipeline:
            pipeline.get("target")
            pipeline.execute()
        assert counter.commits == 3
