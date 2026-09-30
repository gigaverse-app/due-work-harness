"""Real broker controls: an offset determines replay, not what a fake consumer says."""

import asyncio
import os
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import RequestTimedOutError

from due_work_harness.integrations.aiokafka import (
    aiokafka_fenced,
    aiokafka_offset_reply_breaker,
    aiokafka_worker_killer,
)
from due_work_harness.models import DueWorkContractDesignError
from due_work_harness.worker_death import CommitWorker, WorkerDied

BROKER = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:19094")


@pytest.mark.parametrize("fault", ["uncommitted", "death", "lost_reply", "normal", "database_death"])
def test_restart_reads_broker_committed_offset(fault):
    async def history():
        topic, group = f"dwh-{uuid4().hex}", f"dwh-{uuid4().hex}"
        producer = AIOKafkaProducer(bootstrap_servers=BROKER)
        await producer.start()
        try:
            for value in (b"first", b"second"):
                await producer.send_and_wait(topic, value, partition=0)
        finally:
            await producer.stop()

        def consumer():
            return AIOKafkaConsumer(
                bootstrap_servers=BROKER,
                group_id=group,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
            )

        partition = TopicPartition(topic, 0)
        first = consumer()
        first.assign([partition])
        await first.start()
        try:
            message = await asyncio.wait_for(first.getone(), 10)
            assert message.value == b"first"
            if fault == "death":
                with aiokafka_worker_killer(first)(1) as worker:
                    with pytest.raises(WorkerDied):
                        await first.commit({partition: message.offset + 1})
                    assert worker.commits == 1 and worker.dead
                    with pytest.raises(WorkerDied):
                        await first.getone()
                    with pytest.raises(WorkerDied):
                        await first.commit({partition: message.offset + 2})
            elif fault == "database_death":
                worker = CommitWorker(None)
                with aiokafka_fenced(first, worker):
                    with pytest.raises(WorkerDied):
                        worker.kill_now("database committed before offset acknowledgement")
                    with pytest.raises(WorkerDied):
                        await first.commit({partition: message.offset + 1})
            elif fault == "lost_reply":
                with aiokafka_offset_reply_breaker(first)(1) as replies:
                    with pytest.raises(RequestTimedOutError):
                        await first.commit({partition: message.offset + 1})
                    assert replies.count == 1
                    assert (await asyncio.wait_for(first.getone(), 10)).value == b"second"
            elif fault == "normal":
                await first.commit({partition: message.offset + 1})
        finally:
            await first.stop()
        second = consumer()
        second.assign([partition])
        await second.start()
        try:
            replay = await asyncio.wait_for(second.getone(), 10)
            assert replay.value == (b"first" if fault in ("uncommitted", "database_death") else b"second")
        finally:
            await second.stop()

    asyncio.run(history())


def test_auto_commit_cannot_bypass_injected_offset_boundary():
    async def history():
        consumer = AIOKafkaConsumer(bootstrap_servers=BROKER, group_id="unsafe")
        try:
            with pytest.raises(DueWorkContractDesignError, match="auto_commit=False"):
                with aiokafka_worker_killer(consumer)(1):
                    pass
        finally:
            await consumer.stop()

    asyncio.run(history())


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_empty_offset_commit_is_not_a_durable_boundary(explicit_empty):
    async def history():
        consumer = AIOKafkaConsumer(bootstrap_servers=BROKER, group_id=f"empty-{uuid4().hex}", enable_auto_commit=False)
        consumer.assign([TopicPartition(f"empty-{uuid4().hex}", 0)])
        await consumer.start()
        try:
            with aiokafka_worker_killer(consumer)(1) as worker:
                if explicit_empty:
                    # The public driver refuses {}; preserve that error without inventing a commit.
                    with pytest.raises(ValueError):
                        await consumer.commit({})
                else:
                    await consumer.commit()
                assert worker.commits == 0
        finally:
            await consumer.stop()

    asyncio.run(history())


@pytest.mark.parametrize("fetch", ["getone", "getmany"])
def test_fetch_waiting_when_worker_dies_cannot_deliver_later_records(fetch):
    async def history():
        topic = f"fenced-{uuid4().hex}"
        producer = AIOKafkaProducer(bootstrap_servers=BROKER)
        await producer.start()
        consumer = AIOKafkaConsumer(
            bootstrap_servers=BROKER,
            group_id=f"fenced-{uuid4().hex}",
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        consumer.assign([TopicPartition(topic, 0)])
        try:
            await producer.send_and_wait(topic, b"before", partition=0)
            await consumer.start()
            assert (await asyncio.wait_for(consumer.getone(), 10)).value == b"before"
            worker = CommitWorker(None)
            with aiokafka_fenced(consumer, worker):
                operation = consumer.getone() if fetch == "getone" else consumer.getmany(timeout_ms=10000)
                pending = asyncio.create_task(operation)
                try:
                    await asyncio.sleep(0)  # Fetch enters its await before the simulated database death.
                    assert not pending.done()
                    with pytest.raises(WorkerDied):
                        worker.kill_now("database worker died while fetch was waiting")
                    await producer.send_and_wait(topic, b"after", partition=0)
                    with pytest.raises(WorkerDied):
                        await asyncio.wait_for(pending, 10)
                finally:
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
        finally:
            await consumer.stop()
            await producer.stop()

    asyncio.run(history())


@pytest.mark.parametrize("dies", [False, True])
def test_inflight_commit_does_not_resume_a_dead_worker(dies):
    """An already-started broker write may land; its success must not revive the caller."""

    async def history():
        topic, group = f"inflight-{uuid4().hex}", f"inflight-{uuid4().hex}"
        producer = AIOKafkaProducer(bootstrap_servers=BROKER)
        await producer.start()
        try:
            await producer.send_and_wait(topic, b"owed", partition=0)
        finally:
            await producer.stop()
        partition = TopicPartition(topic, 0)
        consumer = AIOKafkaConsumer(
            bootstrap_servers=BROKER, group_id=group, enable_auto_commit=False, auto_offset_reset="earliest"
        )
        consumer.assign([partition])
        await consumer.start()
        try:
            await asyncio.wait_for(consumer.getone(), 10)
            await consumer.commit({partition: 0})  # Establish the real group coordinator.
            worker = CommitWorker(None)
            with aiokafka_fenced(consumer, worker):
                # Hold the driver's actual commit mutex; no broker response or client method is mocked.
                async with consumer._coordinator._commit_lock:
                    pending = asyncio.create_task(consumer.commit({partition: 1}))
                    await asyncio.sleep(0)
                    assert not pending.done()
                    if dies:
                        with pytest.raises(WorkerDied):
                            worker.kill_now("database died while offset commit was awaiting its lock")
                if dies:
                    with pytest.raises(WorkerDied):
                        await asyncio.wait_for(pending, 10)
                else:
                    await asyncio.wait_for(pending, 10)
            assert await consumer.committed(partition) == 1  # In-flight writes cannot be rolled back by a fence.
        finally:
            await consumer.stop()

    asyncio.run(history())
