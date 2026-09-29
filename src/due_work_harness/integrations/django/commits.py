"""
Count a worker's commits on Django's default connection, and kill it inside one.

This is the Django host's :class:`~due_work_harness.host.WorkerKiller`. Inside
the context every commit on the calling thread's default connection is
counted: an outermost ``COMMIT``, a ``COMMIT`` a statement issues itself, and
every autocommit statement that wrote — including a ``SELECT`` that writes
through a function or a zero-row ``UPDATE`` whose predicate does, which
:func:`~due_work_harness.integrations.django.writes.execute_reporting_autocommit_write`
detects from PostgreSQL's transaction-id assignment rather than the statement's
text. An anonymous ``DO`` block, which may commit itself, runs unwrapped and
always counts. Observing never takes over a
transaction someone else owns. A known gap: a write made by ``CALL`` is not
counted, since a procedure may commit itself and so is never wrapped. Right after
the chosen commit the worker dies: callbacks registered with
``transaction.on_commit`` are dropped (Django would run them only later), and
every further statement raises :class:`~due_work_harness.worker_death.WorkerDied`,
so ``finally`` blocks cannot write what a dead process never would. Rollbacks
still run, as the server would roll back a disconnected session anyway. On exit
a dead worker's connection is closed, releasing its advisory locks and session
state.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.base import BaseDatabaseWrapper

from due_work_harness.integrations.django.writes import (
    execute_reporting_autocommit_write,
    leading_keyword,
    require_postgresql,
)
from due_work_harness.worker_death import WorkerDied


class DjangoWorker:
    """The worker a crash history interrupts: its commit count and whether it died."""

    def __init__(self, connection: BaseDatabaseWrapper, kill_after: int | None) -> None:
        self._connection = connection
        self._kill_after = kill_after
        self.commits = 0
        self.dead = False

    def committed(self) -> None:
        self.commits += 1
        if self.commits == self._kill_after:
            self.kill_now(f"worker died right after commit {self.commits}")

    def kill_now(self, reason: str) -> None:
        self.dead = True
        # The process dies before any after-commit callback runs.
        self._connection.run_on_commit.clear()
        raise WorkerDied(reason)

    def refuse_if_dead(self) -> None:
        if self.dead:
            raise WorkerDied("the worker is dead; its connection runs nothing more")


@contextmanager
def django_worker_killer(kill_after: int | None) -> Iterator[DjangoWorker]:
    """Count commits on this thread's default connection; die right after commit ``kill_after``."""
    require_postgresql(DEFAULT_DB_ALIAS, "the commit counter", "it reads transaction-id assignment")
    target = connections[DEFAULT_DB_ALIAS]
    worker = DjangoWorker(target, kill_after)
    original_commit = type(target).commit

    def commit(connection: BaseDatabaseWrapper) -> Any:
        if connection is not target:
            return original_commit(connection)
        worker.refuse_if_dead()
        result = original_commit(connection)
        worker.committed()
        return result

    def statement(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: dict[str, Any]) -> Any:
        if worker.dead and leading_keyword(sql) == "ROLLBACK":
            # Unwinding savepoints after a death mid-transaction: the server would roll back anyway.
            return execute(sql, params, many, context)
        worker.refuse_if_dead()
        # Outside a transaction a write commits by itself; inside one, only a SQL COMMIT is a boundary,
        # and the transaction stays its owner's (a rollback must still roll back).
        result, committed = execute_reporting_autocommit_write(execute, sql, params, many, context)
        if committed:
            worker.committed()
        return result

    try:
        with pytest.MonkeyPatch.context() as patch, target.execute_wrapper(statement):
            patch.setattr(type(target), "commit", commit)
            yield worker
    finally:
        if worker.dead:
            # A dead process's session ends with it: advisory locks, session
            # settings and temporary tables must not survive into recovery, nor
            # into the next test when a history is rejected while its worker is dead.
            target.close()
