"""
The lock-order recorder: what it classifies, what it records per transaction, and the cycles it rejects.

A conforming order passes and a reversed one fails, naming a witness transaction
for every edge of the cycle. The recorder is exercised on real transactions
against real tables, and on the statements Django's ORM actually generates.
"""

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from due_work_harness.integrations.django import lifecycle_references as ref
from due_work_harness.integrations.django import lock_order as lock_order_module
from due_work_harness.integrations.django.lock_order import (
    LockSequence,
    assert_consistent_lock_order,
    locked_tables,
    record_lock_order,
    recording_against_the_ledger,
)

# The fixture the recorder ships, registered the way an adopter's conftest does.
from due_work_harness.integrations.django.lock_order import lock_order as lock_order  # noqa: F401,PLC0414

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ('SELECT "room"."id" FROM "room" WHERE "room"."id" = %s FOR UPDATE', ("room",)),
        ('SELECT * FROM "egress" INNER JOIN "room" ON (...) WHERE x FOR UPDATE', ("egress", "room")),
        ('SELECT * FROM "egress" INNER JOIN "room" ON (...) FOR UPDATE OF "egress"', ("egress",)),
        ('SELECT * FROM "a" JOIN "b" ON (...) FOR NO KEY UPDATE SKIP LOCKED', ("a", "b")),
        ('UPDATE "room" SET "status" = %s WHERE "room"."id" = %s', ("room",)),
        ('DELETE FROM "egress" WHERE "egress"."id" IN (%s)', ("egress",)),
        ('SELECT "room"."id" FROM "room" WHERE "room"."id" = %s', ()),
        ('INSERT INTO "room" ("id") VALUES (%s)', ()),
        # A subquery's rows are read, not locked: FOR UPDATE locks the rows of its own query level.
        (
            'SELECT "work"."id" FROM "work" WHERE "work"."product_id" IN '
            '(SELECT U0."id" FROM "product" U0 WHERE U0."x" = %s) FOR UPDATE',
            ("work",),
        ),
        ('UPDATE "work" SET "s" = 1 WHERE "work"."id" IN (SELECT U0."id" FROM "product" U0)', ("work",)),
        # sqlcommenter and friends put a comment first; PostgreSQL still runs an UPDATE.
        ('/* controller=job */ UPDATE "work" SET "s" = 1', ("work",)),
        ('/* a /* nested */ comment */ SELECT "work"."id" FROM "work" FOR UPDATE', ("work",)),
        # Share locks and table locks are not recorded (documented).
        ('SELECT "work"."id" FROM "work" WHERE "work"."id" = 1 FOR SHARE', ()),
        ('LOCK TABLE "work" IN EXCLUSIVE MODE', ()),
    ],
    ids=[
        "for-update",
        "join",
        "for-update-of",
        "no-key-skip-locked",
        "update",
        "delete",
        "plain-read",
        "insert",
        "subquery-in-select",
        "subquery-in-update",
        "comment-before-update",
        "nested-comment-before-select",
        "for-share",
        "lock-table",
    ],
)
def test_locked_tables_classifies_row_locking_statements(sql: str, expected: tuple[str, ...]) -> None:
    assert locked_tables(sql) == expected


def test_locked_tables_reads_what_the_orm_really_emits() -> None:
    with ref.lifecycle_attempt_table():
        pk = ref.LifecycleAttempt.objects.create(status=ref.Status.RUNNING).pk
        table = ref.LifecycleAttempt._meta.db_table
        with CaptureQueriesContext(connection) as captured, transaction.atomic():
            ref.LifecycleAttempt.objects.select_for_update().get(pk=pk)
            ref.LifecycleAttempt.objects.filter(pk=pk).update(status=ref.Status.COMPLETE)
            ref.LifecycleAttempt.objects.filter(pk=pk).delete()
        classified = [locked_tables(query["sql"]) for query in captured.captured_queries]
    assert [tables for tables in classified if tables] == [(table,), (table,), (table,)]


def test_consistent_orders_pass() -> None:
    assert_consistent_lock_order(
        [
            LockSequence(label="producer", tables=("owner", "work")),
            LockSequence(label="worker", tables=("owner", "work", "receipt")),
            LockSequence(label="reaper", tables=("work", "receipt")),
        ]
    )


def test_a_reversed_order_names_both_witness_transactions() -> None:
    with pytest.raises(AssertionError) as failure:
        assert_consistent_lock_order(
            [
                LockSequence(label="completion", tables=("room", "egress")),
                LockSequence(label="terminal failure", tables=("egress", "room")),
            ]
        )
    message = str(failure.value)
    assert "room -> egress  (in completion)" in message
    assert "egress -> room  (in terminal failure)" in message


def test_a_longer_cycle_is_detected() -> None:
    with pytest.raises(AssertionError, match="deadlock"):
        assert_consistent_lock_order(
            [
                LockSequence(label="one", tables=("a", "b")),
                LockSequence(label="two", tables=("b", "c")),
                LockSequence(label="three", tables=("c", "a")),
            ]
        )


@pytest.fixture
def two_tables() -> Iterator[None]:
    with connection.cursor() as cursor:
        for name in ("lock_order_a", "lock_order_b"):
            cursor.execute(f'CREATE TABLE "{name}" (id integer PRIMARY KEY)')
            cursor.execute(f'INSERT INTO "{name}" VALUES (1)')
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "lock_order_a", "lock_order_b"')


def _lock(*tables: str) -> None:
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f'SELECT id FROM "{table}" WHERE id = 1 FOR UPDATE')


def test_the_first_lock_order_of_each_outermost_transaction_is_recorded(two_tables: None) -> None:
    with record_lock_order("probe") as recorder:
        with transaction.atomic():
            _lock("lock_order_a")
            with transaction.atomic():  # A savepoint does not start a new sequence.
                _lock("lock_order_b", "lock_order_a")
        with transaction.atomic():
            _lock("lock_order_a")  # One table carries no order.
        _lock("lock_order_b", "lock_order_a")  # Outside a transaction nothing is held.
    assert [(item.label, item.tables) for item in recorder.sequences] == [("probe", ("lock_order_a", "lock_order_b"))]


def test_a_reversed_order_across_two_real_transactions_is_rejected(two_tables: None) -> None:
    with record_lock_order("a first") as forward:
        with transaction.atomic():
            _lock("lock_order_a", "lock_order_b")
    with record_lock_order("b first") as reverse:
        with transaction.atomic():
            _lock("lock_order_b", "lock_order_a")
    assert_consistent_lock_order(forward.sequences)
    with pytest.raises(AssertionError, match="b first"):
        assert_consistent_lock_order([*forward.sequences, *reverse.sequences])


def test_another_threads_transaction_neither_starts_nor_ends_a_sequence(two_tables: None) -> None:
    inside, release = threading.Event(), threading.Event()

    def other_thread() -> None:
        try:
            with transaction.atomic():
                inside.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    thread = threading.Thread(target=other_thread)
    with record_lock_order("main") as recorder:
        with transaction.atomic():
            thread.start()
            assert inside.wait(timeout=10)
            _lock("lock_order_a", "lock_order_b")
        # The other thread's block is still open: it must not hold this sequence back.
        sequences_before_release = list(recorder.sequences)
        release.set()
        thread.join(timeout=10)
    assert [item.tables for item in sequences_before_release] == [("lock_order_a", "lock_order_b")]


@pytest.fixture
def fresh_ledger(monkeypatch: pytest.MonkeyPatch) -> list[LockSequence]:
    ledger: list[LockSequence] = []
    monkeypatch.setattr(lock_order_module, "LOCK_ORDER_LEDGER", ledger)
    return ledger


def test_a_later_transaction_is_checked_against_every_earlier_one(
    two_tables: None, fresh_ledger: list[LockSequence]
) -> None:
    with recording_against_the_ledger("first test"):
        with transaction.atomic():
            _lock("lock_order_a", "lock_order_b")
    assert [item.label for item in fresh_ledger] == ["first test"]
    with pytest.raises(AssertionError, match=r"(?s)first test.*second test|second test.*first test"):
        with recording_against_the_ledger("second test"):
            with transaction.atomic():
                _lock("lock_order_b", "lock_order_a")


def test_the_shipped_fixture_records_this_tests_transactions(two_tables: None, lock_order: object) -> None:
    with transaction.atomic():
        _lock("lock_order_a", "lock_order_b")
    assert [item.tables for item in lock_order.sequences] == [("lock_order_a", "lock_order_b")]  # type: ignore[attr-defined]


def test_a_transaction_that_failed_to_open_does_not_silence_the_recorder(two_tables: None) -> None:
    # A durable block nested in another raises from __enter__, and Django never calls its __exit__:
    # counting the entry would leave the recorder one level deep, merging every later transaction.
    with record_lock_order("probe") as recorder:
        with transaction.atomic(), pytest.raises(RuntimeError), transaction.atomic(durable=True):
            pass
        with transaction.atomic():
            _lock("lock_order_a", "lock_order_b")
        with transaction.atomic():
            _lock("lock_order_b", "lock_order_a")
    assert [item.tables for item in recorder.sequences] == [
        ("lock_order_a", "lock_order_b"),
        ("lock_order_b", "lock_order_a"),
    ]
    with pytest.raises(AssertionError, match="deadlock"):
        assert_consistent_lock_order(recorder.sequences)


def test_a_rejected_sequence_does_not_join_the_ledger(two_tables: None, fresh_ledger: list[LockSequence]) -> None:
    # Recorded before the check, the cycle would fail every later test that uses the fixture, in teardown.
    with recording_against_the_ledger("first test"):
        with transaction.atomic():
            _lock("lock_order_a", "lock_order_b")
    with pytest.raises(AssertionError, match="deadlock"), recording_against_the_ledger("second test"):
        with transaction.atomic():
            _lock("lock_order_b", "lock_order_a")
    assert [item.label for item in fresh_ledger] == ["first test"]
    with recording_against_the_ledger("a later test that locks nothing"):
        pass


def test_another_aliass_transaction_neither_starts_nor_ends_a_sequence(
    two_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The recorder watches one alias; Atomic is patched class-wide, so another alias's block must not count.
    # The other alias is simulated: its Atomic enters and exits without a connection.
    real_enter, real_exit = transaction.Atomic.__enter__, transaction.Atomic.__exit__

    def enter(self: transaction.Atomic) -> None:
        return None if self.using == "other" else real_enter(self)

    def exit(self: transaction.Atomic, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None if self.using == "other" else real_exit(self, exc_type, exc_value, traceback)

    monkeypatch.setattr(transaction.Atomic, "__enter__", enter)
    monkeypatch.setattr(transaction.Atomic, "__exit__", exit)
    with record_lock_order("default only") as recorder:
        with transaction.atomic(using="other"):
            with transaction.atomic():
                _lock("lock_order_a")
            with transaction.atomic():
                _lock("lock_order_b")
    # Two one-table transactions on default: no order. Merged under the other alias's block, a false (a, b).
    assert recorder.sequences == []
