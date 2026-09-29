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
  ORM emits. Hand-written SQL is classified only when it quotes its tables
  (``FOR UPDATE OF`` may name an unquoted alias). Leading comments
  (sqlcommenter's, for one) are skipped as PostgreSQL skips them.
* A transaction that locks one table records no order. Two tables are the smallest ordering.
* Only the tables of the locking statement's own query level: a subquery's rows
  are read, not locked, unless the subquery has its own ``FOR UPDATE``, whose
  locks come first. A data-modifying common table expression locks its own
  target, before the statement it feeds. A derived table (``FROM (SELECT ...)``)
  is not classified. String literals (``E''`` escapes too), dollar quotes and
  comments are skipped.
* A lock taken with ``SKIP LOCKED`` or ``NOWAIT`` never waits, so no edge leads
  into a table every lock of which in the transaction was taken that way.
* Exclusive row locks only. ``FOR SHARE``, ``FOR KEY SHARE`` and ``LOCK TABLE``
  are not recorded, though they can take part in a deadlock too.
* Row locks a trigger or a function takes are invisible: they are not in the statement.
* One process. :data:`LOCK_ORDER_LEDGER` holds what this process recorded, so under
  ``pytest-xdist`` two tests on different workers that lock in opposite orders are
  never compared; run the tests that share rows on one worker to compare them.

The locking statements recorded are PostgreSQL's ``FOR [NO KEY] UPDATE [OF ...]``,
``UPDATE [ONLY]``, ``DELETE FROM [ONLY]``, ``MERGE`` and ``INSERT ... ON CONFLICT
DO UPDATE``. A plain ``INSERT`` locks only the rows it creates, which no other
transaction can see yet, so it is not recorded; it can still wait on another
transaction's uncommitted row with the same unique key, and that wait is not
recorded either.
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
_LOCK_OF = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\s+OF\s+((?:(?:\"[^\"]+\"|\w+)(?:\s*,\s*)?)+)", re.IGNORECASE)
_WITHOUT_WAITING = re.compile(r"\b(?:SKIP\s+LOCKED|NOWAIT)\b", re.IGNORECASE)
_NAME = re.compile(r"\"([^\"]+)\"|(\w+)")
#: A table in FROM or JOIN, with the alias the statement gives it, if any.
_FROM_OR_JOIN = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:ONLY\s+)?\"([^\"]+)\"(?:\s+(?:AS\s+)?(?!(?:ON|WHERE|INNER|LEFT|RIGHT|FULL|CROSS|JOIN|"
    r"GROUP|ORDER|LIMIT|FOR|USING|NATURAL|LATERAL|WINDOW|HAVING|UNION|EXCEPT|INTERSECT|OFFSET|FETCH)\b)"
    r"(\"[^\"]+\"|\w+))?",
    re.IGNORECASE,
)
_UPDATE = re.compile(r"UPDATE\s+(?:ONLY\s+)?\"([^\"]+)\"", re.IGNORECASE)
_DELETE = re.compile(r"DELETE\s+FROM\s+(?:ONLY\s+)?\"([^\"]+)\"", re.IGNORECASE)
_MERGE = re.compile(r"MERGE\s+INTO\s+(?:ONLY\s+)?\"([^\"]+)\"", re.IGNORECASE)
_UPSERT = re.compile(r"INSERT\s+INTO\s+\"([^\"]+)\".*\bON\s+CONFLICT\b.*\bDO\s+UPDATE\b", re.IGNORECASE | re.DOTALL)
_SUBQUERY = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
_WITH = re.compile(r"WITH\s+(?:RECURSIVE\s+)?", re.IGNORECASE)
#: A common table expression up to its body's opening parenthesis:
#: ``name [(columns)] AS [NOT] [MATERIALIZED] (``.
_CTE_HEAD = re.compile(
    r"\s*(?:\"[^\"]+\"|\w+)\s*(?:\([^)]*\)\s*)?AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.IGNORECASE
)
_CTE_SEPARATOR = re.compile(r"\s*,")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


class RowLock(HarnessModel):
    """A table whose rows a statement locks, and whether it waits for a row another transaction holds."""

    table: str
    waits: bool = True


def locked_tables(sql: str) -> tuple[str, ...]:
    """Tables whose rows this statement locks, in the order the statement names them."""
    return _distinct([lock.table for lock in row_locks(sql)])


def row_locks(sql: str) -> tuple[RowLock, ...]:
    """
    Each row lock the statement takes, in order, with whether it waits.

    ``SKIP LOCKED`` and ``NOWAIT`` never wait for a lock another transaction
    holds, so a transaction taking one cannot be the one a deadlock leaves waiting.
    """
    return tuple(_locks(_blank_literals_and_comments(statement_text(sql))))


def _locks(text: str) -> list[RowLock]:
    if _WITH.match(text):
        return _locked_by_ctes(text)
    subqueries = [lock for body in _subquery_bodies(text) for lock in _locks(body)]
    for pattern in (_UPDATE, _DELETE, _MERGE):
        if match := pattern.match(text):
            return [*subqueries, RowLock(table=match.group(1))]
    if match := _UPSERT.match(text):
        # The conflicting row is locked for the update, waiting for another transaction's.
        return [*subqueries, RowLock(table=match.group(1))]
    top_level = _outside_parentheses(text)
    if not _LOCKING_SELECT.search(top_level):
        return subqueries
    waits = not _WITHOUT_WAITING.search(top_level)
    tables = _FROM_OR_JOIN.findall(top_level)
    if scoped := _LOCK_OF.search(top_level):
        aliases = {_unquoted(alias) or table: table for table, alias in tables} | {table: table for table, _ in tables}
        named = [quoted or bare for quoted, bare in _NAME.findall(scoped.group(1))]
        chosen = [aliases.get(name, name) for name in named]
    else:
        chosen = [table for table, _ in tables]
    return [*subqueries, *(RowLock(table=table, waits=waits) for table in _distinct(chosen))]


def _unquoted(name: str) -> str:
    return name[1:-1] if name.startswith('"') else name


def _subquery_bodies(text: str) -> list[str]:
    """Every parenthesised ``SELECT`` at this level, whose own ``FOR UPDATE`` locks rows too."""
    bodies, position = [], 0
    for opening in _SUBQUERY.finditer(text):
        if opening.start() < position:
            continue
        start = opening.start() + 1
        end = _closing_parenthesis(text, start)
        bodies.append(text[start:end].strip())
        position = end
    return bodies


def _locked_by_ctes(text: str) -> list[RowLock]:
    """A ``WITH`` statement: each common table expression's locks in order, then the statement they feed."""
    with_clause = _WITH.match(text)
    assert with_clause is not None
    locked: list[RowLock] = []
    position = with_clause.end()
    while head := _CTE_HEAD.match(text, position):
        end = _closing_parenthesis(text, head.end())
        locked.extend(_locks(text[head.end() : end].strip()))
        position = end + 1
        comma = _CTE_SEPARATOR.match(text, position)
        if comma is None:
            break
        position = comma.end()
    locked.extend(_locks(text[position:].strip()))
    return locked


def _closing_parenthesis(text: str, start: int) -> int:
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _distinct(tables: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for table in tables:
        if table not in seen:
            seen.append(table)
    return tuple(seen)


def _blank_literals_and_comments(sql: str) -> str:
    """
    The statement with string literals, dollar quotes and comments blanked out, so their text opens nothing.

    Quoted identifiers are kept: they are the table names. Blanking keeps every
    other character where it was.
    """
    out, index = list(sql), 0
    while index < len(sql):
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            end = len(sql) if end < 0 else end
        elif sql.startswith("/*", index):
            end, depth = index + 2, 1
            while end < len(sql) and depth:
                if sql.startswith("/*", end):
                    depth, end = depth + 1, end + 2
                elif sql.startswith("*/", end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
        elif sql[index] == "'":
            # An E'' literal takes backslash escapes; a plain one only doubles its quote.
            escapes = (
                index > 0
                and sql[index - 1] in "Ee"
                and (index < 2 or not (sql[index - 2].isalnum() or sql[index - 2] == "_"))
            )
            end = index + 1
            while end < len(sql):
                if escapes and sql[end] == "\\":
                    end += 2
                elif sql[end] == "'" and sql.startswith("''", end):
                    end += 2
                elif sql[end] == "'":
                    end += 1
                    break
                else:
                    end += 1
        elif sql[index] == '"':
            closing = sql.find('"', index + 1)
            index = len(sql) if closing < 0 else closing + 1
            continue
        elif tag := _DOLLAR_TAG.match(sql, index):
            closing = sql.find(tag.group(), tag.end())
            end = len(sql) if closing < 0 else closing + len(tag.group())
        else:
            index += 1
            continue
        out[index:end] = " " * (end - index)
        index = end
    return "".join(out)


def _outside_parentheses(sql: str) -> str:
    """The statement's own query level: every parenthesised group (a subquery, a JOIN's ON) blanked out."""
    depth, kept, quoted = 0, [], False
    for character in sql:
        if character == '"':
            quoted = not quoted
        if quoted or character == '"':
            kept.append(character if depth == 0 else " ")
            continue
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
    #: Tables every lock of which, in this transaction, was taken without waiting (``SKIP LOCKED``,
    #: ``NOWAIT``): the transaction never waits for them, so no edge leads into them.
    without_waiting: frozenset[str] = frozenset()


#: Sequences recorded by the ``lock_order`` fixture in this process, so a later
#: test's transactions are checked against earlier ones.
LOCK_ORDER_LEDGER: list[LockSequence] = []


class LockOrderRecorder(MutableHarnessModel):
    """The sequences recorded so far; also the execute wrapper that records them."""

    label: str
    using: str = DEFAULT_DB_ALIAS
    sequences: list[LockSequence] = []
    _current: list[str] | None = PrivateAttr(default=None)
    _waited: set[str] = PrivateAttr(default_factory=set)
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
            locks = row_locks(sql)
            if locks:
                if self._current is None:
                    self._current = []
                for lock in locks:
                    if lock.table not in self._current:
                        self._current.append(lock.table)
                    if lock.waits:
                        self._waited.add(lock.table)
        return execute(sql, params, many, context)

    def enter(self) -> None:
        if threading.get_ident() != self._thread:
            return
        if self._depth == 0:
            self._current, self._waited = None, set()
        self._depth += 1

    def exit(self) -> None:
        if threading.get_ident() != self._thread:
            return
        self._depth -= 1
        if self._depth == 0 and self._current is not None:
            if len(self._current) > 1:
                self.sequences.append(
                    LockSequence(
                        label=self.label,
                        tables=tuple(self._current),
                        without_waiting=frozenset(self._current) - self._waited,
                    )
                )
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
                if second in sequence.without_waiting:
                    continue  # never waited for, so this transaction cannot be stuck waiting on it
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
