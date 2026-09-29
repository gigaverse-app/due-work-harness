"""
Record row-lock acquisition order per transaction, and reject inconsistent orders.

Two code paths that lock the same tables in opposite orders deadlock only under
concurrency, which sequential tests never produce. This recorder needs no
concurrency: it watches the SQL each transaction issues, notes the order in
which tables are first row-locked (``SELECT ... FOR UPDATE``, ``UPDATE``,
``DELETE``), and turns every recorded transaction into "table A before table B"
edges. A cycle in that graph is a pair of transactions, or a longer ring of
them, that can deadlock each other, and the failure names a witness
transaction for each edge.

Use :func:`record_lock_order` around production paths that share rows, or the
``lock_order`` pytest fixture, and finish with
:func:`assert_consistent_lock_order`. The fixture checks each test's
transactions against every sequence recorded earlier in the process
(:data:`LOCK_ORDER_LEDGER`), so two tests that each look fine on their own can
still be caught disagreeing. Register the fixture in a top-level ``conftest.py``::

    from due_work_harness.integrations.django.lock_order import lock_order  # noqa: F401

What it sees, and what it does not:

* Outermost ``transaction.atomic()`` blocks on one database alias, on the
  thread that started recording. A savepoint does not start a new sequence; a
  block that fails to open (a nested ``durable=True``) is not counted; another
  alias's blocks are ignored; a transaction opened with ``set_autocommit(False)``
  or raw ``BEGIN`` is not seen.
* Only statements Django generates with quoted identifiers, which is what the
  ORM emits. Hand-written SQL is classified only when it quotes its tables.
  Leading comments (sqlcommenter's, for one) are skipped as PostgreSQL skips them.
* A transaction that locks one table records no order. Two tables are the smallest ordering.
* Only the tables of the locking statement's own query level: a subquery's rows
  are read, not locked. A derived table (``FROM (SELECT ...)``) is not classified.
* Exclusive row locks only. ``FOR SHARE``, ``FOR KEY SHARE`` and ``LOCK TABLE``
  are not recorded, though they can take part in a deadlock too.
* Row locks a trigger or a function takes are invisible: they are not in the statement.

PostgreSQL's ``FOR [NO KEY] UPDATE [OF ...]`` and the writes ``UPDATE`` and
``DELETE`` are the locking statements; ``INSERT`` takes no lock on rows it does not read.
"""

import re
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from graphlib import CycleError, TopologicalSorter
from itertools import pairwise
from typing import Any
from unittest.mock import patch

import pytest
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from pydantic import PrivateAttr

from due_work_harness.integrations.django.writes import statement_text
from due_work_harness.models import HarnessModel, MutableHarnessModel

_LOCKING_SELECT = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\b", re.IGNORECASE)
_LOCK_OF = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\s+OF\s+((?:\"[^\"]+\"(?:\s*,\s*)?)+)", re.IGNORECASE)
_QUOTED = re.compile(r"\"([^\"]+)\"")
_FROM_OR_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+\"([^\"]+)\"", re.IGNORECASE)
_UPDATE = re.compile(r"UPDATE\s+\"([^\"]+)\"", re.IGNORECASE)
_DELETE = re.compile(r"DELETE\s+FROM\s+\"([^\"]+)\"", re.IGNORECASE)


def locked_tables(sql: str) -> tuple[str, ...]:
    """Tables whose rows this statement locks, in the order the statement names them."""
    text = statement_text(sql)
    if (match := _UPDATE.match(text)) or (match := _DELETE.match(text)):
        return (match.group(1),)
    top_level = _outside_parentheses(text)
    if not _LOCKING_SELECT.search(top_level):
        return ()
    if scoped := _LOCK_OF.search(top_level):
        return tuple(_QUOTED.findall(scoped.group(1)))
    seen: list[str] = []
    for table in _FROM_OR_JOIN.findall(top_level):
        if table not in seen:
            seen.append(table)
    return tuple(seen)


def _outside_parentheses(sql: str) -> str:
    """The statement's own query level: every parenthesised group (a subquery, a JOIN's ON) blanked out."""
    depth, kept = 0, []
    for character in sql:
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            kept.append(character)
            continue
        kept.append(" ")
    return "".join(kept)


class LockSequence(HarnessModel):
    """Distinct tables in first-lock order within one outermost transaction."""

    label: str
    tables: tuple[str, ...]


#: Sequences recorded by the ``lock_order`` fixture in this process, so a later
#: test's transactions are checked against earlier ones.
LOCK_ORDER_LEDGER: list[LockSequence] = []


class LockOrderRecorder(MutableHarnessModel):
    """The sequences recorded so far; also the execute wrapper that records them."""

    label: str
    using: str = DEFAULT_DB_ALIAS
    sequences: list[LockSequence] = []
    _current: list[str] | None = PrivateAttr(default=None)
    _depth: int = PrivateAttr(default=0)
    _thread: int = PrivateAttr(default_factory=threading.get_ident)

    def __call__(
        self,
        execute: Callable[[str, Any, bool, dict[str, Any]], Any],
        sql: str,
        params: Any,
        many: bool,
        context: dict[str, Any],
    ) -> Any:
        if connections[self.using].in_atomic_block:
            tables = locked_tables(sql)
            if tables:
                if self._current is None:
                    self._current = []
                for table in tables:
                    if table not in self._current:
                        self._current.append(table)
        return execute(sql, params, many, context)

    def enter(self) -> None:
        if threading.get_ident() != self._thread:
            return
        if self._depth == 0:
            self._current = None
        self._depth += 1

    def exit(self) -> None:
        if threading.get_ident() != self._thread:
            return
        self._depth -= 1
        if self._depth == 0 and self._current is not None:
            if len(self._current) > 1:
                self.sequences.append(LockSequence(label=self.label, tables=tuple(self._current)))
            self._current = None


@contextmanager
def record_lock_order(label: str, using: str = DEFAULT_DB_ALIAS) -> Iterator[LockOrderRecorder]:
    """
    Observe outermost transactions on one database alias; production code is unchanged.

    ``transaction.Atomic`` is patched for the duration so nesting is known, and
    only entries and exits on the calling thread count: another thread's
    ``atomic()`` block neither starts nor ends a sequence here.
    """
    recorder = LockOrderRecorder(label=label, using=using)
    original_enter = transaction.Atomic.__enter__
    original_exit = transaction.Atomic.__exit__

    def watched(atomic: transaction.Atomic) -> bool:
        return (atomic.using or DEFAULT_DB_ALIAS) == using

    def enter(self: transaction.Atomic) -> None:
        # Counted only once open: a block whose __enter__ raises never reaches __exit__.
        result = original_enter(self)
        if watched(self):
            recorder.enter()
        return result

    def exit(self: transaction.Atomic, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            return original_exit(self, exc_type, exc_value, traceback)
        finally:
            if watched(self):
                recorder.exit()

    with (
        patch.object(transaction.Atomic, "__enter__", enter),
        patch.object(transaction.Atomic, "__exit__", exit),
        connections[using].execute_wrapper(recorder),
    ):
        yield recorder


def assert_consistent_lock_order(sequences: Iterable[LockSequence]) -> None:
    """Every pair of tables must be locked in one global order across all recorded transactions."""
    witness: dict[tuple[str, str], LockSequence] = {}
    # A table's predecessors: every table some transaction locked before it.
    graph = TopologicalSorter[str]()
    for sequence in sequences:
        for index, first in enumerate(sequence.tables):
            for second in sequence.tables[index + 1 :]:
                witness.setdefault((first, second), sequence)
                graph.add(second, first)
    try:
        graph.prepare()
    except CycleError as error:
        # Each node of the reported cycle precedes the next; the first is repeated at the end.
        cycle: list[str] = error.args[1]
        lines = [f"  {first} -> {second}  (in {witness[(first, second)].label})" for first, second in pairwise(cycle)]
        raise AssertionError(
            "inconsistent row-lock order; these transactions can deadlock each other:\n" + "\n".join(lines)
        ) from None


@contextmanager
def recording_against_the_ledger(label: str, using: str = DEFAULT_DB_ALIAS) -> Iterator[LockOrderRecorder]:
    """
    Record, then check what was recorded against every sequence recorded before it.

    A later test that reverses an earlier one fails, naming both. Only sequences
    that pass join :data:`LOCK_ORDER_LEDGER`: a rejected cycle kept there would
    fail every later test that checks against it.
    """
    with record_lock_order(label, using) as recorder:
        yield recorder
    assert_consistent_lock_order([*LOCK_ORDER_LEDGER, *recorder.sequences])
    LOCK_ORDER_LEDGER.extend(recorder.sequences)


@pytest.fixture
def lock_order(request: pytest.FixtureRequest) -> Iterator[LockOrderRecorder]:
    """
    Record this test's row-lock order and check it against every sequence recorded so far.

    Opt in from any test that drives a production path sharing rows with other
    production paths; the check runs when the test finishes.
    """
    with recording_against_the_ledger(request.node.nodeid) as recorder:
        yield recorder
