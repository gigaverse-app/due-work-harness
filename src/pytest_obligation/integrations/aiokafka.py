"""Faults at aiokafka's acknowledged consumer-offset boundary.

This adapter supports AIOKafkaConsumer only, not other Kafka client libraries.
Use a real consumer with auto-commit disabled. The broker commits first; only
then can the worker die or lose its reply. Restart with the same group to observe
what actually replays. Application handlers, partition watermarks, serialization
and retry policy remain owned by the application. These are consumer offset
faults, not Kafka transaction or producer-delivery proofs.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from pytest_obligation.models import ObligationContractDesignError
from pytest_obligation.worker_death import CommitWorker, LostCommitReplies


@contextmanager
def _watching(consumer: Any, before: Callable[[], None], committed: Callable[[], None]) -> Iterator[None]:
    import pytest
    from aiokafka import AIOKafkaConsumer

    if not isinstance(consumer, AIOKafkaConsumer) or consumer._enable_auto_commit:
        raise ObligationContractDesignError(
            "aiokafka offset histories require AIOKafkaConsumer(enable_auto_commit=False)"
        )
    original_commit = consumer.commit
    original_getone = consumer.getone
    original_getmany = consumer.getmany

    async def commit(offsets: Any = None) -> None:
        before()
        # Resolve the same snapshot as AIOKafkaConsumer.commit before its first await.
        # An empty mapping makes the coordinator return without writing any offsets.
        committed_offsets = offsets
        if offsets is None:
            subscription = consumer._subscription.subscription
            if subscription is not None and subscription.assignment is not None:
                committed_offsets = subscription.assignment.all_consumed_offsets()
        await original_commit(offsets)
        before()
        if committed_offsets:
            committed()

    async def getone(*args: Any, **kwargs: Any) -> Any:
        before()
        result = await original_getone(*args, **kwargs)
        before()
        return result

    async def getmany(*args: Any, **kwargs: Any) -> Any:
        before()
        result = await original_getmany(*args, **kwargs)
        before()
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(consumer, "commit", commit)
        patch.setattr(consumer, "getone", getone)
        patch.setattr(consumer, "getmany", getmany)
        yield


@contextmanager
def aiokafka_fenced(consumer: Any, worker: CommitWorker) -> Iterator[None]:
    """Share a database worker's death fence: cleanup must not commit Kafka offsets after it dies."""
    with _watching(consumer, worker.refuse_if_dead, lambda: None):
        yield


def aiokafka_worker_killer(consumer: Any) -> Callable[[int | None], AbstractContextManager[CommitWorker]]:
    """Fence this consumer's fetches and commits after an acknowledged offset commit kills it."""

    @contextmanager
    def killer(kill_after: int | None) -> Iterator[CommitWorker]:
        worker = CommitWorker(kill_after)
        with _watching(consumer, worker.refuse_if_dead, worker.committed):
            yield worker

    return killer


class AIOKafkaLostOffsetReplies(LostCommitReplies):
    """Lose an acknowledged offset reply using aiokafka's timeout exception."""

    def __init__(self, lose_at: int | None) -> None:
        from aiokafka.errors import RequestTimedOutError

        super().__init__(
            lose_at, lambda count: RequestTimedOutError(f"Kafka offset commit {count} landed but its reply was lost")
        )


def aiokafka_offset_reply_breaker(
    consumer: Any,
) -> Callable[[int | None], AbstractContextManager[AIOKafkaLostOffsetReplies]]:
    @contextmanager
    def breaker(lose_at: int | None) -> Iterator[AIOKafkaLostOffsetReplies]:
        replies = AIOKafkaLostOffsetReplies(lose_at)
        with _watching(consumer, lambda: None, replies.committed):
            yield replies

    return breaker
