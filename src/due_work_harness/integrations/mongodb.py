"""Interrupt acknowledged MongoDB writes on one PyMongo client (or Motor's delegate).

Observe the driver's wire-command boundary, not collection methods or command
monitoring callbacks (PyMongo swallows exceptions from monitoring listeners).
Single-document writes and each bulk wire batch count after acknowledgement;
transactional writes count only at commitTransaction. Reads and aborts do not
count. Unknown commands conservatively count as writes. Unacknowledged writes
are refused: there is no acknowledged boundary at which to kill the worker.

The checkout scope follows Motor into its executor thread. All operations on
the selected client are fenced after death, including reads and finally-block
writes. Other clients remain usable by independent provider/observer code.
This integration supports synchronous PyMongo 4.9+ and Motor 3.7+ via .delegate;
native AsyncMongoClient requires a separate async wire adapter. It neither
freezes server TTLs nor claims MongoDB query-plan or Kafka delivery coverage.
"""

from collections.abc import Callable, Collection, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import Any

from due_work_harness.host import Host
from due_work_harness.models import DueWorkContractDesignError
from due_work_harness.worker_death import CommitWorker, LostCommitReplies

# Anything not positively known to be read-only counts conservatively. Aggregation
# has its own rule because $out/$merge are writes despite using a read-shaped API.
_READ_COMMANDS = frozenset(
    {
        "find",
        "getMore",
        "killCursors",
        "count",
        "distinct",
        "ping",
        "hello",
        "isMaster",
        "ismaster",
        "listCollections",
        "listIndexes",
        "listDatabases",
        "explain",
        "collStats",
        "dbStats",
        "abortTransaction",
    }
)


def _writes(spec: dict[str, Any]) -> bool:
    name = next(iter(spec))
    if name == "aggregate":
        return any("$out" in stage or "$merge" in stage for stage in spec.get("pipeline", ()))
    return name not in _READ_COMMANDS


@contextmanager
def _watching(client: Any, before: Callable[[], None], committed: Callable[[], None]) -> Iterator[None]:
    import pytest
    from pymongo.synchronous.mongo_client import MongoClient
    from pymongo.synchronous.pool import Connection

    if not isinstance(client, MongoClient):
        raise DueWorkContractDesignError("pass a synchronous PyMongo client, or Motor client.delegate")
    # A separate context per watcher lets the killer and reply breaker compose.
    active: ContextVar[tuple[bool, Any]] = ContextVar("mongodb_checkout", default=(False, None))
    original_checkout = MongoClient._checkout
    original_command = Connection.command
    original_write = Connection.write_command
    original_send = Connection.send_message
    original_unack = Connection.unack_write

    @contextmanager
    def checkout(self: Any, server: Any, session: Any) -> Iterator[Any]:
        if self is client:
            before()
        with original_checkout(self, server, session) as connection:
            token = active.set((self is client, session))
            try:
                yield connection
            finally:
                active.reset(token)

    def command(self: Any, dbname: str, spec: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        watched, session = active.get()
        if watched:
            before()
            concern = kwargs.get("write_concern")
            if spec.get("writeConcern", {}).get("w") == 0 or (concern is not None and not concern.acknowledged):
                raise DueWorkContractDesignError("MongoDB histories require acknowledged writes (w >= 1)")
        in_transaction = session is not None and session.in_transaction
        result = original_command(self, dbname, spec, *args, **kwargs)
        if watched and _writes(spec) and (not in_transaction or "commitTransaction" in spec):
            committed()
        return result

    def write_command(self: Any, *args: Any, **kwargs: Any) -> Any:
        watched, session = active.get()
        if watched:
            before()
        in_transaction = session is not None and session.in_transaction
        result = original_write(self, *args, **kwargs)
        if watched and not in_transaction:
            committed()
        return result

    def send_message(self: Any, *args: Any, **kwargs: Any) -> Any:
        if active.get()[0]:
            before()
        return original_send(self, *args, **kwargs)

    def unack_write(self: Any, *args: Any, **kwargs: Any) -> Any:
        if active.get()[0]:
            raise DueWorkContractDesignError("MongoDB histories require acknowledged writes (w >= 1)")
        return original_unack(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MongoClient, "_checkout", checkout)
        patch.setattr(Connection, "command", command)
        patch.setattr(Connection, "write_command", write_command)
        patch.setattr(Connection, "send_message", send_message)
        patch.setattr(Connection, "unack_write", unack_write)
        yield


MongoDBWorker = CommitWorker


def mongodb_worker_killer(client: Any) -> Callable[[int | None], AbstractContextManager[MongoDBWorker]]:
    @contextmanager
    def killer(kill_after: int | None) -> Iterator[MongoDBWorker]:
        worker = MongoDBWorker(kill_after)
        with _watching(client, worker.refuse_if_dead, worker.committed):
            yield worker

    return killer


class MongoDBLostReplies(LostCommitReplies):
    """An acknowledged write loses its reply with the driver's connection error."""

    def __init__(self, lose_at: int | None) -> None:
        from pymongo.errors import ConnectionFailure

        super().__init__(
            lose_at, lambda count: ConnectionFailure(f"MongoDB reply to write {count} lost; the write landed")
        )


def mongodb_reply_breaker(client: Any) -> Callable[[int | None], AbstractContextManager[MongoDBLostReplies]]:
    @contextmanager
    def breaker(lose_at: int | None) -> Iterator[MongoDBLostReplies]:
        replies = MongoDBLostReplies(lose_at)
        with _watching(client, lambda: None, replies.committed):
            yield replies

    return breaker


def mongodb_host(client: Any, production_packages: Collection[str]) -> Host:
    """Configure actual write/death and lost-reply proofs; other capabilities stay explicit."""
    return Host(
        production_packages=frozenset(production_packages),
        worker_killer=mongodb_worker_killer(client),
        reply_breaker=mongodb_reply_breaker(client),
    )
