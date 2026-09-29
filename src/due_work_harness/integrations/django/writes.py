"""
Classify a statement that changed rows by what PostgreSQL reports, not by its text.

A prefix check on the SQL (``INSERT``/``UPDATE``/``DELETE``) misses a data-
modifying CTE (``WITH … INSERT``), a leading comment, ``MERGE`` and ``COPY``.
The server's command status (psycopg's ``statusmessage``) names the command
that actually ran and how many rows it affected, whatever the text looked
like. Harness observers that must not miss a write classify it here.
"""

import re
from collections.abc import Callable
from typing import Any

from django.db import connections

from due_work_harness.models import HarnessModel

#: Commands whose status reports rows written. ``COPY`` counts: ``COPY FROM``
#: inserts rows, and ``COPY TO`` never reaches an execute wrapper as a write.
_ROW_WRITES = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "COPY"})
_CREATES_ROWS = frozenset({"INSERT", "MERGE", "COPY"})
#: Statements that can write without their own status saying so: a function called from a
#: ``SELECT``, or a trigger or a predicate function called from a DML statement that then
#: affects zero rows. Everything else reports honestly, or refuses a transaction block.
_TRANSACTIONAL_STATEMENTS = frozenset({"SELECT", "WITH", "VALUES", "INSERT", "UPDATE", "DELETE", "MERGE"})


class RowWrite(HarnessModel):
    """A statement that changed at least one row."""

    command: str
    rows: int
    target: str

    @property
    def creates_rows(self) -> bool:
        """Whether the command can bring a row into existence (MERGE may insert)."""
        return self.command in _CREATES_ROWS

    def __str__(self) -> str:
        return f"{self.command} {self.target}"


def _target(sql: str) -> str:
    """Best-effort table name for messages; classification never depends on it."""
    words = sql.replace("(", " ").split()
    upper = [word.upper() for word in words]
    for keyword in ("INTO", "UPDATE", "FROM"):
        if keyword in upper and upper.index(keyword) + 1 < len(words):
            return words[upper.index(keyword) + 1].strip('"')
    return "?"


def leading_keyword(sql: str) -> str:
    """The first SQL keyword after comments, whitespace and opening parentheses."""
    keyword = re.match(r"[a-zA-Z]+", statement_text(sql))
    return keyword.group().upper() if keyword else ""


def statement_text(sql: str) -> str:
    """
    The statement as PostgreSQL reads it: leading comments, whitespace and opening parentheses removed.

    Empty when a leading block comment never closes. The one place the harness strips
    SQL comments, so every reader of a statement's first word agrees on it.
    """
    text = sql.lstrip()
    while True:
        if text.startswith("/*"):
            # PostgreSQL permits nested block comments; the first closing
            # delimiter need not finish the leading comment.
            depth = 1
            for delimiter in re.finditer(r"/\*|\*/", text[2:]):
                depth += 1 if delimiter.group() == "/*" else -1
                if depth == 0:
                    text = text[delimiter.end() + 2 :].lstrip()
                    break
            else:
                return ""
        elif text.startswith("--"):
            text = text.partition("\n")[2].lstrip()
        elif text.startswith("("):
            text = text[1:].lstrip()
        else:
            return text


def require_postgresql(alias: str, reading: str, because: str) -> None:
    """
    Refuse, in words, a proof that reads PostgreSQL-specific state on another database.

    Without this a SQLite run fails on a SQL error from inside the harness
    (``pg_current_xact_id_if_assigned`` does not exist), which reads as a
    harness bug rather than as a database the proof cannot serve.
    """
    vendor = connections[alias].vendor
    assert vendor == "postgresql", (
        f"{reading} needs PostgreSQL ({because}), and database {alias!r} is {vendor!r}. "
        f"Run this proof against PostgreSQL, or configure the host for this database"
    )


def execute_reporting_autocommit_write(
    execute: Callable[..., Any], sql: str, params: Any, many: bool, context: dict[str, Any]
) -> tuple[Any, bool]:
    """
    Run one statement without taking over its transaction, and report whether it committed a write.

    A write made inside a function reports the command status of the ``SELECT``
    that called it (``SELECT procrastinate_defer_jobs_v1(...)`` reads as
    ``SELECT 1``), so :func:`row_write` cannot see it. A trigger or a predicate
    can also write while the outer ``UPDATE`` or ``DELETE`` affects zero rows.
    In autocommit, statements that can write either way therefore run in an
    explicit single-statement transaction, and PostgreSQL's transaction-id
    assignment decides: an xid is assigned exactly when the transaction wrote
    (a row lock or an explicit allocation assigns one too, so this can only
    over-count a boundary, never miss one). That is autocommit's own semantics
    made observable. Other statements report through their command status,
    which also keeps utility statements that refuse a transaction block out of one.

    A transaction someone else owns is never begun or ended here. The server's
    own transaction status is consulted as well as Django's autocommit flag,
    because a raw ``BEGIN`` does not update the flag. Inside such a transaction
    the statement runs as written, and only a SQL ``COMMIT`` (or ``END``) counts,
    at the boundary where it happens.
    """
    connection = context["connection"]
    database = connection.connection
    assert database is not None, "an executing Django statement must have an open PostgreSQL connection"
    # psycopg 2 reports an int and psycopg 3 an IntEnum; IDLE is 0 in both.
    in_transaction = int(database.info.transaction_status) != 0
    if in_transaction or not connection.get_autocommit():
        result = execute(sql, params, many, context)
        return result, in_transaction and context["cursor"].statusmessage == "COMMIT"
    if leading_keyword(sql) not in _TRANSACTIONAL_STATEMENTS:
        result = execute(sql, params, many, context)
        return result, row_write(sql, context["cursor"]) is not None
    _run(database, "BEGIN")
    try:
        result = execute(sql, params, many, context)
        (wrote,) = _run(database, "SELECT pg_current_xact_id_if_assigned() IS NOT NULL", fetch=True)
    except BaseException:
        _run(database, "ROLLBACK")
        raise
    _run(database, "COMMIT")
    return result, bool(wrote)


def _run(database: Any, sql: str, *, fetch: bool = False) -> Any:
    """
    One statement on the raw driver connection, through a DB-API cursor.

    Every PostgreSQL driver has ``cursor()``; only psycopg 3 also has the
    ``connection.execute()`` shortcut, and Django runs on psycopg2 too.
    """
    with database.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchone() if fetch else None


def row_write(sql: str, cursor: Any) -> RowWrite | None:
    """
    The rows the statement just executed on ``cursor`` wrote, or ``None``.

    ``cursor`` is the Django cursor an execute wrapper receives; its
    ``statusmessage`` comes from the psycopg cursor underneath (for example
    ``"INSERT 0 1"``, ``"UPDATE 3"``). A statement that affected no rows is not
    a write: a guarded ``UPDATE … WHERE`` that matches nothing changed nothing.
    """
    status = (cursor.statusmessage or "").split()
    if not status or status[0] not in _ROW_WRITES:
        return None
    rows = int(status[-1]) if status[-1].isdigit() else 0
    return RowWrite(command=status[0], rows=rows, target=_target(sql)) if rows else None
