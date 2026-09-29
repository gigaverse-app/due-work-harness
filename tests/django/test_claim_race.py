"""
Profile B's two-connection race through the Django host, against a real PostgreSQL table.

The host gives each racing thread its own connection, bounded by a statement
timeout and closed on every exit. A claim that locks its row wins once; one
that reads before it writes wins twice; one blocked on a lock nothing releases
fails the proof, with the timeout's error, instead of leaving one winner and a
green result.
"""

import threading
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from django.db import connection, transaction

from due_work_harness.integrations import django as django_integration
from due_work_harness.integrations.django import django_host
from due_work_harness.integrations.django import lifecycle_references as ref
from due_work_harness.profiles.bounded_ownership import (
    FencedOwnership,
    assert_claim_is_exclusive_across_connections,
)

pytestmark = pytest.mark.django_db(transaction=True)
Status = ref.Status
_LOCK = 4242


@pytest.fixture(autouse=True)
def attempt_table() -> Iterator[None]:
    with ref.lifecycle_attempt_table():
        yield


def _make_requested() -> int:
    return ref.LifecycleAttempt.objects.create(status=Status.REQUESTED).pk


def _owner(claim: object, make_claimable: object = _make_requested) -> FencedOwnership:
    # Only the race proof runs here, and it uses just these two bindings: the rest are inert.
    return FencedOwnership(
        name="reference attempts",
        make_claimable=make_claimable,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        fenced_write=lambda row, token: False,
        renew_lease=lambda row, token: False,
        expire_lease=lambda row: None,
        reclaim_stalled=lambda row: None,
    )


def _claim_locking_the_row() -> tuple[int, UUID] | None:
    with transaction.atomic():
        row = ref.LifecycleAttempt.objects.select_for_update(skip_locked=True).filter(status=Status.REQUESTED).first()
        if row is None:
            return None
        ref.LifecycleAttempt.objects.filter(pk=row.pk).update(status=Status.RUNNING)
        return row.pk, uuid4()


def test_a_claim_that_locks_its_row_wins_once() -> None:
    assert_claim_is_exclusive_across_connections(_owner(_claim_locking_the_row))


def test_a_claim_that_reads_before_it_writes_wins_twice() -> None:
    # Each racer waits, between its read and its write, for the other to have read: the double claim is certain.
    both_have_read = threading.Barrier(2, timeout=10)

    def claim_without_locking() -> tuple[int, UUID] | None:
        row = ref.LifecycleAttempt.objects.filter(status=Status.REQUESTED).first()
        if row is None:
            return None
        both_have_read.wait()
        ref.LifecycleAttempt.objects.filter(pk=row.pk).update(status=Status.RUNNING)
        return row.pk, uuid4()

    with pytest.raises(AssertionError, match="2 of 2 concurrent claims won"):
        assert_claim_is_exclusive_across_connections(_owner(claim_without_locking))


def test_a_claim_blocked_on_a_lock_fails_the_proof_instead_of_hanging_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(django_integration, "RACER_STATEMENT_TIMEOUT_MS", 500)

    def hold_the_lock_the_claim_needs() -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [_LOCK])
        return _make_requested()

    def claim_behind_the_lock() -> tuple[int, UUID] | None:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK])
        return None

    try:
        with pytest.raises(AssertionError, match=r"raised instead of losing cleanly.*statement timeout"):
            assert_claim_is_exclusive_across_connections(
                _owner(claim_behind_the_lock, hold_the_lock_the_claim_needs), timeout=5
            )
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock_all()")


def test_a_racing_threads_connection_is_bounded_and_closed_even_when_persistent() -> None:
    seen: dict[str, object] = {}

    def racer() -> None:
        # A persistent connection: close_old_connections would leave it open with its thread gone.
        connection.settings_dict["CONN_MAX_AGE"] = None
        with django_host(production_packages=set()).connection_scope():
            with connection.cursor() as cursor:
                cursor.execute("SHOW statement_timeout")
                seen["timeout"] = cursor.fetchone()[0]
            seen["open_inside"] = connection.connection is not None
        seen["closed_after"] = connection.connection is None

    thread = threading.Thread(target=racer)
    thread.start()
    thread.join(timeout=30)
    assert seen == {"timeout": "10s", "open_inside": True, "closed_after": True}
