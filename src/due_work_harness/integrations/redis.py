"""
Redis integration: count a worker's commits on a Redis client, kill it inside one, or lose one's reply.

A queue that keeps its state in Redis has no transaction to commit, but it has
the same boundaries a crash can fall between: each round trip that writes. Every
write on the client's connection pool counts as one commit:

* a pipeline's ``execute()``, transactional (``MULTI``/``EXEC``) or not, when it
  holds a write — a pipeline travels in one round trip, so a process that dies
  sends all of it or none of it;
* a standalone command whose server-side flags (``COMMAND``) include
  ``write``, such as ``LMOVE``, ``ZADD`` or ``SET``;
* a script (``EVAL``, ``EVALSHA``, ``FCALL``): the server cannot say whether a
  script writes, so each counts, and a read-only one only adds a history that
  must converge anyway.

:func:`redis_worker_killer` is the host's
:class:`~due_work_harness.host.WorkerKiller`: right after the chosen commit the
worker dies, and every later command on that pool raises
:class:`~due_work_harness.worker_death.WorkerDied`, so ``finally`` blocks and
cleanup cannot write what a dead process never would. What the server holds
stays: keys with a TTL keep counting down, as they would after a real death.

:func:`redis_reply_breaker` is the host's
:class:`~due_work_harness.host.ReplyBreaker`: the chosen commit lands, then the
client raises ``redis.exceptions.ConnectionError``, as it does when the
connection drops before the answer arrives. The worker lives on.

What neither sees: commands on another connection pool, and a worker that forks
(RQ's default ``Worker`` runs each job in a child process). Run the worker in
process, as ``SimpleWorker`` does, so its commands pass through here.

:func:`redis_host` builds a host for a project whose due work lives in Redis
and that has no database for the harness to watch.
"""

from collections.abc import Callable, Collection, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from due_work_harness.host import Host, PublicationBreaker, ReceiverBreaker
from due_work_harness.integrations.clocks import time_machine_clock
from due_work_harness.worker_death import WorkerDied

#: Commands whose writes the server cannot report in advance.
SCRIPT_COMMANDS = frozenset({"EVAL", "EVALSHA", "FCALL"})


def _command_name(args: tuple[Any, ...]) -> str:
    name = args[0]
    return (name.decode() if isinstance(name, bytes) else str(name)).upper().split(" ", 1)[0]


class _Writes:
    """Which commands write, as the server reports it for this client's server."""

    def __init__(self, client: Any, execute_command: Callable[..., Any]) -> None:
        self._client = client
        self._execute_command = execute_command
        self._flags: dict[str, frozenset[str]] = {}

    def __call__(self, args: tuple[Any, ...]) -> bool:
        if not self._flags:
            for name, info in self._execute_command(self._client, "COMMAND").items():
                self._flags[name.upper()] = frozenset(info.get("flags", ()))
        name = _command_name(args)
        return name in SCRIPT_COMMANDS or "write" in self._flags.get(name, frozenset())


@contextmanager
def _watching(client: Any, before: Callable[[], None], committed: Callable[[], None]) -> Iterator[None]:
    """
    Call ``before`` ahead of every command on ``client``'s pool, and ``committed`` after each that wrote.

    Patches stack: a watcher entered inside another wraps it, so a killer and a
    reply breaker both see every commit.
    """
    import pytest
    from redis.client import Pipeline, Redis

    pool = client.connection_pool
    original_command = Redis.execute_command
    # The unwatched command, so that asking the server for its flags is neither counted nor refused.
    unwatched = getattr(original_command, "unwatched", original_command)
    writes = _Writes(client, unwatched)
    original_immediate = Pipeline.immediate_execute_command
    original_execute = Pipeline.execute

    def execute_command(self: Any, *args: Any, **options: Any) -> Any:
        if self.connection_pool is not pool:
            return original_command(self, *args, **options)
        before()
        result = original_command(self, *args, **options)
        if writes(args):
            committed()
        return result

    def immediate_execute_command(self: Any, *args: Any, **options: Any) -> Any:
        if self.connection_pool is not pool:
            return original_immediate(self, *args, **options)
        before()
        result = original_immediate(self, *args, **options)
        if writes(args):
            committed()
        return result

    def execute(self: Any, raise_on_error: bool = True) -> Any:
        if self.connection_pool is not pool:
            return original_execute(self, raise_on_error)
        before()
        wrote = any(writes(args) for args, _options in self.command_stack)
        result = original_execute(self, raise_on_error)
        if wrote:
            committed()
        return result

    execute_command.unwatched = unwatched  # type: ignore[attr-defined]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Redis, "execute_command", execute_command)
        patch.setattr(Pipeline, "immediate_execute_command", immediate_execute_command)
        patch.setattr(Pipeline, "execute", execute)
        yield


class RedisWorker:
    """The worker a crash history interrupts: its commit count and whether it died."""

    def __init__(self, kill_after: int | None) -> None:
        self._kill_after = kill_after
        self.commits = 0
        self.dead = False

    def committed(self) -> None:
        self.commits += 1
        if self.commits == self._kill_after:
            self.kill_now(f"worker died right after commit {self.commits}")

    def kill_now(self, reason: str) -> None:
        self.dead = True
        raise WorkerDied(reason)

    def refuse_if_dead(self) -> None:
        if self.dead:
            raise WorkerDied("the worker is dead; its connection sends nothing more")


def redis_worker_killer(client: Any) -> Callable[[int | None], AbstractContextManager[RedisWorker]]:
    """A worker killer for every client and pipeline sharing ``client``'s connection pool."""

    @contextmanager
    def killer(kill_after: int | None) -> Iterator[RedisWorker]:
        worker = RedisWorker(kill_after)
        with _watching(client, worker.refuse_if_dead, worker.committed):
            yield worker

    return killer


class LostReplies:
    """The commits a crash history counts, and the connection error raised for the chosen one's reply."""

    def __init__(self, lose_at: int | None) -> None:
        self._lose_at = lose_at
        self.count = 0
        self.failure: Exception | None = None

    def committed(self) -> None:
        from redis.exceptions import ConnectionError as RedisConnectionError

        self.count += 1
        if self.count == self._lose_at:
            self.failure = RedisConnectionError(
                f"Connection closed by server (the reply to commit {self.count} was lost; the write landed)"
            )
            raise self.failure


def redis_reply_breaker(client: Any) -> Callable[[int | None], AbstractContextManager[LostReplies]]:
    """A reply breaker for every client and pipeline sharing ``client``'s connection pool."""

    @contextmanager
    def breaker(lose_at: int | None) -> Iterator[LostReplies]:
        replies = LostReplies(lose_at)
        with _watching(client, lambda: None, replies.committed):
            yield replies

    return breaker


def redis_host(
    client: Any,
    production_packages: Collection[str],
    *,
    publication_breaker: PublicationBreaker | None = None,
    receiver_breaker: ReceiverBreaker | None = None,
) -> Host:
    """
    A host for a project whose due work lives in Redis, reached through ``client``.

    It supplies the worker killer and reply breaker above and, when
    ``time-machine`` is installed, a frozen clock for proofs that age work past
    a recovery delay. Redis's own key expiry follows the server's clock, which
    no frozen clock moves.
    """
    return Host(
        production_packages=frozenset(production_packages),
        worker_killer=redis_worker_killer(client),
        reply_breaker=redis_reply_breaker(client),
        publication_breaker=publication_breaker,
        receiver_breaker=receiver_breaker,
        frozen_clock=time_machine_clock,
    )


__all__ = ["LostReplies", "RedisWorker", "redis_host", "redis_reply_breaker", "redis_worker_killer"]
