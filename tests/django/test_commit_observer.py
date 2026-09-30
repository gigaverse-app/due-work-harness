"""
The Django worker killer's commit observer: what it counts as a commit, and what it leaves to its owner.

A commit counted where none happened, or missed where one did, moves every
crash history's boundaries. And an observer that begins or ends a transaction
it does not own changes what the code under test does: a rollback that no
longer rolls back proves nothing about the handoff being rolled back.
"""

import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.db import connection, transaction

from due_work_harness.crash_histories import HandoffHistory, crash_histories
from due_work_harness.integrations.django import lifecycle_references as ref
from due_work_harness.integrations.django.commits import django_worker_killer
from due_work_harness.integrations.django.writes import leading_keyword
from due_work_harness.worker_death import WorkerDied

pytestmark = pytest.mark.django_db(transaction=True)
Status = ref.Status


@pytest.fixture(autouse=True)
def attempt_table() -> Iterator[None]:
    with ref.lifecycle_attempt_table():
        yield


def _running() -> int:
    return ref.LifecycleAttempt.objects.create(status=Status.RUNNING).pk


def _status(pk: int) -> str:
    return ref.LifecycleAttempt.objects.get(pk=pk).status


@pytest.mark.parametrize("manual", [False, True], ids=["atomic", "manual-transaction"])
def test_observing_a_read_preserves_the_callers_rollback(manual: bool) -> None:
    pk = _running()

    def write_then_read() -> None:
        ref.LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)
        assert _status(pk) == Status.COMPLETE

    with django_worker_killer(None) as worker:
        if manual:
            transaction.set_autocommit(False)
            try:
                write_then_read()
            finally:
                transaction.rollback()
                transaction.set_autocommit(True)
        else:
            with transaction.atomic():
                write_then_read()
                transaction.set_rollback(True)
    assert _status(pk) == Status.RUNNING
    assert worker.commits == 0


@pytest.mark.parametrize("commit", [False, True], ids=["rollback", "commit"])
@pytest.mark.parametrize("crash_after", [None, 1], ids=["count", "kill"])
def test_a_transaction_the_sql_manages_keeps_its_own_boundary(commit: bool, crash_after: int | None) -> None:
    # A raw BEGIN does not update Django's flags: only the server knows a transaction is open.
    pk = _running()
    with django_worker_killer(crash_after) as worker:
        try:
            with connection.cursor() as cursor:
                cursor.execute("BEGIN")
                try:
                    ref.LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)
                    assert _status(pk) == Status.COMPLETE
                finally:
                    cursor.execute("COMMIT" if commit else "ROLLBACK")
        except WorkerDied:
            assert commit and crash_after == 1
    assert _status(pk) == (Status.COMPLETE if commit else Status.RUNNING)
    assert worker.commits == int(commit)
    assert worker.dead == (commit and crash_after == 1)


@pytest.mark.parametrize("prefix", ["SELECT ", "SELECT(", "/* outer /* inner */ outer */ SELECT "])
def test_a_function_write_counts_however_the_sql_is_spelled(prefix: str) -> None:
    pk = _running()
    suffix = ")" if prefix.endswith("(") else ""
    with django_worker_killer(None) as worker, connection.cursor() as cursor:
        cursor.execute(f"{prefix}{ref.FAIL_ATTEMPT_FUNCTION}(%s){suffix}", [pk])
    assert _status(pk) == Status.RETRYABLE_FAILED
    assert worker.commits == 1


@pytest.mark.parametrize(
    ("sql", "keyword"),
    [
        ("/* /* */ */ UPDATE t SET a = 1", "UPDATE"),
        ("/* unterminated /* SELECT 1 */", ""),
        ("-- a comment\n(SELECT 1)", "SELECT"),
        ("select(1)", "SELECT"),
        ("", ""),
    ],
)
def test_the_leading_keyword_reads_what_postgresql_reads(sql: str, keyword: str) -> None:
    assert leading_keyword(sql) == keyword


@pytest.mark.parametrize("command", ["UPDATE", "DELETE"])
def test_a_function_write_in_a_zero_row_statement_counts_as_a_commit(command: str) -> None:
    pk = _running()
    table = ref.LifecycleAttempt._meta.db_table
    statement = f"UPDATE {table} SET status = status" if command == "UPDATE" else f"DELETE FROM {table}"
    # The predicate calls a writing function, then rejects the row: PostgreSQL reports
    # UPDATE 0 / DELETE 0 although the function wrote.
    with django_worker_killer(None) as worker, connection.cursor() as cursor:
        cursor.execute(
            f"{statement} WHERE id = %s AND {ref.FAIL_ATTEMPT_FUNCTION}(id)::text = 'never matches void'", [pk]
        )
        assert cursor.rowcount == 0
    assert _status(pk) == Status.RETRYABLE_FAILED
    assert worker.commits == 1


@pytest.mark.parametrize("command", ["UPDATE", "DELETE"])
def test_a_zero_row_statement_that_wrote_nothing_is_not_a_commit(command: str) -> None:
    # The negative direction: wrapping in a transaction must not turn a no-op into a boundary.
    pk = _running()
    table = ref.LifecycleAttempt._meta.db_table
    statement = f"UPDATE {table} SET status = 'complete'" if command == "UPDATE" else f"DELETE FROM {table}"
    with django_worker_killer(None) as worker, connection.cursor() as cursor:
        cursor.execute(f"{statement} WHERE id = %s AND status = 'no such status'", [pk])
        assert cursor.rowcount == 0
    assert _status(pk) == Status.RUNNING
    assert worker.commits == 0


def test_a_dead_workers_session_is_closed_when_a_history_is_rejected_while_it_is_dead() -> None:
    # A history that fails its own validation raises out of the worker's context; the dead
    # session must still end, or its advisory locks and settings leak into the next test.
    pk = _running()
    session = connection.connection
    assert session is not None
    try:
        with pytest.raises(AssertionError, match="invalid history"), django_worker_killer(1):
            try:
                ref.LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)
            except WorkerDied:
                raise AssertionError("invalid history") from None
        assert session.closed
    finally:
        connection.close()


def test_a_write_in_a_do_block_counts_as_a_commit() -> None:
    # DO reports "DO" whatever its body wrote, and may run inside a transaction block, so it is wrapped.
    pk = _running()
    table = ref.LifecycleAttempt._meta.db_table
    with django_worker_killer(None) as worker, connection.cursor() as cursor:
        cursor.execute(f"DO $$ BEGIN UPDATE {table} SET status = 'complete' WHERE id = {pk}; END $$")
    assert _status(pk) == Status.COMPLETE
    assert worker.commits == 1


def test_a_do_block_that_commits_itself_runs_unchanged_and_counts() -> None:
    # A DO block may COMMIT, which it cannot do inside a transaction block: it runs as written, and counts.
    pk = _running()
    table = ref.LifecycleAttempt._meta.db_table
    with django_worker_killer(None) as worker, connection.cursor() as cursor:
        cursor.execute(
            f"DO $$ BEGIN UPDATE {table} SET status = 'retryable_failed' WHERE id = {pk}; COMMIT; "
            f"UPDATE {table} SET status = 'complete' WHERE id = {pk}; END $$"
        )
    assert _status(pk) == Status.COMPLETE
    assert worker.commits == 1


def test_a_do_block_counts_as_a_commit_even_when_it_wrote_nothing() -> None:
    # Unwrapped, a DO's writes cannot be seen, so it is counted conservatively: one crash point too many,
    # never one missed.
    pk = _running()
    with django_worker_killer(None) as worker, connection.cursor() as cursor:
        cursor.execute("DO $$ BEGIN PERFORM 1; END $$")
    assert _status(pk) == Status.RUNNING
    assert worker.commits == 1


def test_a_do_block_inside_the_callers_transaction_is_not_a_commit() -> None:
    pk = _running()
    table = ref.LifecycleAttempt._meta.db_table
    with django_worker_killer(None) as worker:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"DO $$ BEGIN UPDATE {table} SET status = 'complete' WHERE id = {pk}; END $$")
        assert worker.commits == 1  # the atomic block's own COMMIT, not the DO
    assert _status(pk) == Status.COMPLETE


@pytest.mark.xfail(
    strict=True,
    reason="known gap: CALL may commit inside its procedure, so it cannot be wrapped in a one-statement "
    "transaction, and its status reports CALL whatever it wrote",
)
def test_a_write_in_a_called_procedure_counts_as_a_commit() -> None:
    pk = _running()
    table = ref.LifecycleAttempt._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE PROCEDURE due_work_harness_complete(attempt_id bigint) LANGUAGE sql AS "
            f"$$ UPDATE {table} SET status = 'complete' WHERE id = attempt_id $$"
        )
    try:
        with django_worker_killer(None) as worker, connection.cursor() as cursor:
            cursor.execute("CALL due_work_harness_complete(%s)", [pk])
        assert _status(pk) == Status.COMPLETE
        assert worker.commits == 1
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP PROCEDURE due_work_harness_complete(bigint)")


def _executor_threads() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread.name.startswith("ThreadPoolExecutor")}


@pytest.mark.parametrize(
    "transition",
    [ref.fail_attempt_through_async_to_sync, ref.fail_attempt_through_async_to_sync_collecting_on_the_way_out],
    ids=["unwinding-quietly", "collected-while-unwinding"],
)
def test_a_crash_history_through_async_to_sync_leaves_no_executor_thread_behind(
    transition: Callable[[int], Any],
) -> None:
    # The history catches each death it injects; what the death's traceback held must go with the dead worker,
    # even when a young collection ran while it unwound and promoted the cycle out of generation 0.
    before = _executor_threads()
    history = HandoffHistory(name="retryable failure", arrange=_running, transition=transition, observe=_status)
    crash_histories(ref.RETRY_DELIVERY, history)
    for thread in _executor_threads() - before:
        thread.join(timeout=5)  # a released executor's thread exits on its own, promptly
    assert _executor_threads() <= before
