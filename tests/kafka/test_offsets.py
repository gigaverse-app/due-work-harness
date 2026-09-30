"""Real broker controls: an offset determines replay, not what a fake consumer says."""

import asyncio
import os
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import RequestTimedOutError

from due_work_harness.integrations.kafka import kafka_fenced, kafka_offset_reply_breaker, kafka_worker_killer
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
                with kafka_worker_killer(first)(1) as worker:
                    with pytest.raises(WorkerDied):
                        await first.commit({partition: message.offset + 1})
                    assert worker.commits == 1 and worker.dead
                    with pytest.raises(WorkerDied):
                        await first.getone()
                    with pytest.raises(WorkerDied):
                        await first.commit({partition: message.offset + 2})
            elif fault == "database_death":
                worker = CommitWorker(None)
                with kafka_fenced(first, worker):
                    with pytest.raises(WorkerDied):
                        worker.kill_now("database committed before offset acknowledgement")
                    with pytest.raises(WorkerDied):
                        await first.commit({partition: message.offset + 1})
            elif fault == "lost_reply":
                with kafka_offset_reply_breaker(first)(1) as replies:
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
                with kafka_worker_killer(consumer)(1):
                    pass
        finally:
            await consumer.stop()

    asyncio.run(history())
