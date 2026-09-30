"""Faults at aiokafka's acknowledged consumer-offset boundary.

Use a real consumer with auto-commit disabled. The broker commits first; only
then can the worker die or lose its reply. Restart with the same group to observe
what actually replays. Application handlers, partition watermarks, serialization
and retry policy remain owned by the application. These are consumer offset
faults, not Kafka transaction or producer-delivery proofs.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from due_work_harness.models import DueWorkContractDesignError
from due_work_harness.worker_death import CommitWorker


@contextmanager
def _watching(consumer: Any, before: Callable[[], None], committed: Callable[[], None]) -> Iterator[None]:
    import pytest
    from aiokafka import AIOKafkaConsumer

    if not isinstance(consumer, AIOKafkaConsumer) or consumer._enable_auto_commit:
        raise DueWorkContractDesignError("Kafka offset histories require AIOKafkaConsumer(enable_auto_commit=False)")
    original_commit = consumer.commit
    original_getone = consumer.getone
    original_getmany = consumer.getmany

    async def commit(*args: Any, **kwargs: Any) -> None:
        before()
        await original_commit(*args, **kwargs)
        committed()

    async def getone(*args: Any, **kwargs: Any) -> Any:
        before()
        return await original_getone(*args, **kwargs)

    async def getmany(*args: Any, **kwargs: Any) -> Any:
        before()
        return await original_getmany(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(consumer, "commit", commit)
        patch.setattr(consumer, "getone", getone)
        patch.setattr(consumer, "getmany", getmany)
        yield


@contextmanager
def kafka_fenced(consumer: Any, worker: CommitWorker) -> Iterator[None]:
    """Share a database worker's death fence: cleanup must not commit Kafka offsets after it dies."""
    with _watching(consumer, worker.refuse_if_dead, lambda: None):
        yield


def kafka_worker_killer(consumer: Any) -> Callable[[int | None], AbstractContextManager[CommitWorker]]:
    """Fence this consumer's fetches and commits after an acknowledged offset commit kills it."""

    @contextmanager
    def killer(kill_after: int | None) -> Iterator[CommitWorker]:
        worker = CommitWorker(kill_after)
        with _watching(consumer, worker.refuse_if_dead, worker.committed):
            yield worker

    return killer


class KafkaLostOffsetReplies:
    """Count offset acknowledgements; the chosen one raises the driver's timeout after landing."""

    def __init__(self, lose_at: int | None) -> None:
        self._lose_at = lose_at
        self.count = 0
        self.failure: Exception | None = None

    def committed(self) -> None:
        from aiokafka.errors import RequestTimedOutError

        self.count += 1
        if self.count == self._lose_at:
            self.failure = RequestTimedOutError("Kafka offset commit landed but its reply was lost")
            raise self.failure


def kafka_offset_reply_breaker(consumer: Any) -> Callable[[int | None], AbstractContextManager[KafkaLostOffsetReplies]]:
    @contextmanager
    def breaker(lose_at: int | None) -> Iterator[KafkaLostOffsetReplies]:
        replies = KafkaLostOffsetReplies(lose_at)
        with _watching(consumer, lambda: None, replies.committed):
            yield replies

    return breaker
