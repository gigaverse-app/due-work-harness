"""Real-server controls for MongoDB boundaries, including Motor's executor threads."""

import asyncio
import os
from uuid import uuid4

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from pymongo.write_concern import WriteConcern

from due_work_harness import CallableDelivery, ExternalCall, HandoffHistory, assert_crash_at_every_commit_converges
from due_work_harness.crash_histories import HistoriesDiverged
from due_work_harness.host import hosted
from due_work_harness.integrations.mongodb import mongodb_host, mongodb_reply_breaker, mongodb_worker_killer
from due_work_harness.models import DueWorkContractDesignError
from due_work_harness.worker_death import WorkerDied
from tests_support.mongodb_app import MongoDBOutbox


@pytest.fixture
def client():
    with MongoClient(
        os.environ.get("MONGODB_URI", "mongodb://localhost:27028/?directConnection=true"), retryWrites=False
    ) as client:
        client.admin.command("ping")
        yield client


@pytest.fixture
def collection(client):
    collection = client[f"due_work_{uuid4().hex}"].work
    yield collection
    client.drop_database(collection.database.name)


@pytest.mark.parametrize("write", ["insert", "update", "findAndModify", "bulk"])
def test_death_occurs_after_the_write_and_blocks_finally_reads_and_writes(client, collection, write):
    collection.insert_one({"_id": "before"})
    with mongodb_worker_killer(client)(1) as worker:
        with pytest.raises(WorkerDied):
            if write == "insert":
                collection.insert_one({"_id": "owed"})
            elif write == "update":
                collection.update_one({"_id": "owed"}, {"$set": {"due": True}}, upsert=True)
            elif write == "findAndModify":
                collection.find_one_and_update({"_id": "owed"}, {"$set": {"due": True}}, upsert=True)
            else:
                collection.insert_many([{"_id": "owed"}, {"_id": "other"}])
        assert worker.commits == 1
        with pytest.raises(WorkerDied):
            collection.find_one({})
        with pytest.raises(WorkerDied):
            collection.insert_one({"_id": "cleanup"})
    assert collection.find_one({"_id": "owed"}) is not None
    assert collection.find_one({"_id": "cleanup"}) is None


def test_reads_do_not_count_and_an_unwatched_client_can_observe_after_death(client, collection):
    with MongoClient(client.address[0], client.address[1], directConnection=True) as observer:
        with mongodb_worker_killer(client)(1) as worker:
            assert collection.find_one({}) is None
            assert list(collection.aggregate([{"$match": {}}])) == []
            assert worker.commits == 0
            with pytest.raises(WorkerDied):
                collection.insert_one({"_id": "owed"})
            assert observer[collection.database.name].work.find_one({"_id": "owed"}) is not None


def test_lost_reply_leaves_the_write_and_the_worker_alive(client, collection):
    with mongodb_reply_breaker(client)(1) as replies:
        with pytest.raises(ConnectionFailure, match="reply"):
            collection.insert_one({"_id": "owed"})
        assert collection.find_one({"_id": "owed"}) is not None
        collection.insert_one({"_id": "next"})
        assert replies.count == 2


def test_transaction_counts_only_the_commit_and_abort_is_not_a_commit(client, collection):
    collection.insert_one({"_id": "seed"})
    with mongodb_worker_killer(client)(1) as worker, client.start_session() as session:
        session.start_transaction()
        collection.insert_one({"_id": "aborted"}, session=session)
        session.abort_transaction()
        assert worker.commits == 0
        session.start_transaction()
        collection.insert_one({"_id": "owed"}, session=session)
        collection.update_one({"_id": "owed"}, {"$set": {"due": True}}, session=session)
        assert worker.commits == 0
        with pytest.raises(WorkerDied):
            session.commit_transaction()
    assert collection.find_one({"_id": "aborted"}) is None
    assert collection.find_one({"_id": "owed"})["due"] is True


def test_unacknowledged_writes_are_refused_before_they_can_escape(client, collection):
    with mongodb_worker_killer(client)(None):
        with pytest.raises(DueWorkContractDesignError, match="acknowledged"):
            collection.with_options(write_concern=WriteConcern(w=0)).insert_one({"_id": "escape"})
    assert collection.find_one({"_id": "escape"}) is None


def test_motor_writes_are_counted_on_the_delegate_client(collection):
    async def exercise():
        client = AsyncIOMotorClient(os.environ.get("MONGODB_URI", "mongodb://localhost:27028/?directConnection=true"))
        try:
            coll = client[collection.database.name].work
            with mongodb_worker_killer(client.delegate)(1) as worker:
                with pytest.raises(WorkerDied):
                    await coll.insert_one({"_id": "owed"})
                assert worker.commits == 1
                with pytest.raises(WorkerDied):
                    await coll.find_one({})
            assert await coll.find_one({"_id": "owed"}) is not None
        finally:
            client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("deduplicates", [True, False])
def test_generated_histories_catch_a_repeated_provider_effect(client, collection, deduplicates):
    effects = []

    def send(identity):
        if not deduplicates or identity not in effects:
            effects.append(identity)

    app = MongoDBOutbox(collection, send)

    def arrange():
        collection.delete_many({})
        effects.clear()
        return "order"

    history = HandoffHistory(
        name="persist, notify, settle",
        arrange=arrange,
        transition=app.place,
        observe=lambda identity: (collection.count_documents({"done": True}), tuple(effects)),
        external_calls=(ExternalCall(app, "send"),),
    )
    with hosted(mongodb_host(client, {"tests_support.mongodb_app"})):
        if deduplicates:
            assert_crash_at_every_commit_converges(CallableDelivery(name="outbox", recover=app.recover), history)
        else:
            with pytest.raises(HistoriesDiverged, match="external call"):
                assert_crash_at_every_commit_converges(CallableDelivery(name="outbox", recover=app.recover), history)
