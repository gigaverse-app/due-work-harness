"""
Crash histories through the Django host, against a real PostgreSQL table.

Negative controls are the handoff shapes a retrying pipeline can take: a
failure whose successor commits in a second transaction, in two autocommit
writes, through a SQL function whose write reports ``SELECT 1``, in a commit
hook, in a ``finally`` block, or only as a message. Each must diverge without
the adopter naming a boundary. Positive controls commit the failure and its
successor together, including inside a savepoint. Deaths after a named
external call find a notification that recovery repeats, and converge once the
recipient honours an idempotency key. Failing each after-commit callback finds
a handoff that a failing *earlier* callback skips, and shows what ``robust=True``
does and does not fix. Refusing each Celery publication, as a broker that is down
would, finds a handoff behind an earlier publish. Agreement alone is not a pass: a recovery that recovers
nothing fails.
"""

from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone

from due_work_harness.crash_histories import (
    CallableDelivery,
    Delivery,
    ExternalCall,
    HandoffHistory,
    assert_crash_at_every_commit_converges,
    crash_histories,
)
from due_work_harness.integrations.django import lifecycle_references as ref

pytestmark = pytest.mark.django_db(transaction=True)
Status = ref.Status


@pytest.fixture(autouse=True)
def attempt_table() -> Iterator[None]:
    with ref.lifecycle_attempt_table():
        yield


def _arrange() -> int:
    # ARRANGE: a running attempt about to fail retryably, untouched for an hour.
    return ref.LifecycleAttempt.objects.create(status=Status.RUNNING, updated_at=timezone.now() - timedelta(hours=1)).pk


def _observe(pk: int) -> tuple[str, tuple[str, ...]]:
    # OBSERVE: the failed attempt's state and every successor it handed off to.
    successors = ref.LifecycleAttempt.objects.filter(retry_of_id=pk).order_by("pk")
    return ref.LifecycleAttempt.objects.get(pk=pk).status, tuple(successor.status for successor in successors)


def _history(transition: Callable[[int], Any], name: str = "retryable failure") -> HandoffHistory[int, Any]:
    return HandoffHistory(name=name, arrange=_arrange, transition=transition, observe=_observe)


def _converges(delivery: Delivery, history: HandoffHistory[int, Any]) -> None:
    assert_crash_at_every_commit_converges(delivery, history)


@pytest.mark.parametrize(
    "transition",
    [ref.fail_attempt_with_atomic_handoff, ref.fail_attempt_with_savepoint_handoff],
    ids=["atomic", "atomic-with-savepoint"],
)
def test_an_atomic_handoff_converges_after_a_death_after_its_commit(transition: Callable[[int], Any]) -> None:
    _converges(ref.RETRY_DELIVERY, _history(transition))


@pytest.mark.parametrize(
    ("transition", "divergence"),
    [
        (ref.fail_attempt_with_split_handoff, r"'worker died after commit 1': \('retryable_failed', \(\)\)"),
        (ref.fail_attempt_in_autocommit, r"'worker died after commit 1': \('retryable_failed', \(\)\)"),
        (
            ref.fail_attempt_through_a_function_then_hand_off,
            r"'worker died after commit 1': \('retryable_failed', \(\)\)",
        ),
        (ref.fail_attempt_with_commit_hook_handoff, r"'worker died after commit 1': \('retryable_failed', \(\)\)"),
        (ref.fail_attempt_with_finally_handoff, r"'worker died after commit 1': \('retryable_failed', \(\)\)"),
        (ref.fail_attempt_with_message_handoff, r"'notifications lost': \('retryable_failed', \(\)\)"),
    ],
    ids=[
        "split-transactions",
        "autocommit-split",
        "function-write-split",
        "commit-hook",
        "finally-block",
        "message-only",
    ],
)
def test_a_handoff_a_death_or_lost_message_can_strand_diverges(
    transition: Callable[[int], Any], divergence: str
) -> None:
    with pytest.raises(AssertionError, match=divergence):
        _converges(ref.RETRY_DELIVERY, _history(transition))


def test_the_function_write_counts_as_a_commit() -> None:
    # Without transaction-id evidence only the successor's INSERT would count, and a
    # death after it converges; with it, the failure's SELECT is commit 1.
    runs = crash_histories(ref.RETRY_DELIVERY, _history(ref.fail_attempt_through_a_function_then_hand_off))
    assert runs[0].commits == 2


def test_a_transition_that_changes_nothing_fails_the_positive_control() -> None:
    with pytest.raises(
        AssertionError, match="positive control failed: normal operation left the observation unchanged"
    ):
        _converges(ref.RETRY_DELIVERY, _history(ref.reconcile_attempt, name="heartbeat"))


def test_agreement_with_an_inert_recovery_fails_the_positive_control() -> None:
    with pytest.raises(AssertionError, match="recovery changed the observation in none of them"):
        _converges(ref.INERT_DELIVERY, _history(ref.fail_attempt_with_atomic_handoff))


def test_a_test_authored_transition_is_refused() -> None:
    def authored(pk: int) -> None:
        ref.LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)

    with pytest.raises(AssertionError, match=r"handoff\[authored\]\.transition authors production semantics"):
        _converges(ref.RETRY_DELIVERY, _history(authored, name="authored"))


def test_a_test_authored_recovery_is_refused() -> None:
    def authored_recovery() -> None:
        ref.LifecycleAttempt.objects.filter(status=Status.REQUESTED).update(status=Status.RUNNING)

    delivery = CallableDelivery(name="authored", recover=authored_recovery)
    with pytest.raises(AssertionError, match=r"recover authors production semantics"):
        _converges(delivery, _history(ref.fail_attempt_with_atomic_handoff))


def test_a_transition_that_survives_its_death_fails_the_history() -> None:
    with pytest.raises(AssertionError, match="kept running after its worker died"):
        _converges(ref.RETRY_DELIVERY, _history(ref.fail_attempt_swallowing_the_death))


def test_a_dead_workers_session_ends_with_it() -> None:
    _converges(ref.RETRY_DELIVERY, _history(ref.fail_attempt_atomically_under_session_lock))
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        (held,) = cursor.fetchone()
    assert held == 0


def _running_attempt() -> int:
    return ref.LifecycleAttempt.objects.create(status=Status.RUNNING, updated_at=timezone.now() - timedelta(hours=1)).pk


def _observe_notifications(pk: int) -> tuple[str, int]:
    return ref.LifecycleAttempt.objects.get(pk=pk).status, ref.RECIPIENT.received.count(pk)


def _notifying(transition: Callable[[int], Any], seam: str) -> HandoffHistory[int, Any]:
    return HandoffHistory(
        name="completion",
        arrange=_running_attempt,
        transition=transition,
        observe=_observe_notifications,
        external_calls=(ExternalCall(ref.RECIPIENT, seam),),
    )


@pytest.mark.parametrize(
    "transition",
    [ref.complete_attempt_notifying, ref.complete_attempt_notifying_inside_its_transaction],
    ids=["after-the-call", "inside-the-transaction"],
)
def test_a_notification_recovery_repeats_diverges(transition: Callable[[int], Any]) -> None:
    with pytest.raises(AssertionError, match=r"'worker died after external call 1': \('complete', 2\).*repeated"):
        _converges(ref.NOTIFYING_DELIVERY, _notifying(transition, "notify"))


def test_an_idempotent_notification_converges() -> None:
    _converges(ref.NOTIFYING_ONCE_DELIVERY, _notifying(ref.complete_attempt_notifying_once, "notify_once"))


def test_naming_a_seam_the_transition_never_calls_is_refused() -> None:
    with pytest.raises(AssertionError, match="made none of them"):
        _converges(ref.NOTIFYING_ONCE_DELIVERY, _notifying(ref.complete_attempt_notifying_once, "notify"))


def test_a_handoff_committed_with_its_state_survives_every_failing_callback() -> None:
    runs = crash_histories(ref.RETRY_DELIVERY, _history(ref.fail_attempt_atomically_then_publishing))
    assert [run.label for run in runs if run.label.startswith("after-commit")] == ["after-commit callback 1 failed"]
    _converges(ref.RETRY_DELIVERY, _history(ref.fail_attempt_atomically_then_publishing))


def test_a_failing_earlier_callback_skips_the_handoff_after_it() -> None:
    with pytest.raises(AssertionError) as divergence:
        _converges(ref.RETRY_DELIVERY, _history(ref.fail_attempt_publishing_after_an_audit))
    assert "'after-commit callback 1 failed': ('retryable_failed', ())" in str(divergence.value)
    assert "'after-commit callback 2 failed': ('retryable_failed', ())" in str(divergence.value)


def test_a_handoff_committed_with_its_state_survives_a_refused_publication() -> None:
    runs = crash_histories(ref.RETRY_DELIVERY, _history(ref.fail_attempt_atomically_then_notifying))
    assert [run.label for run in runs if run.label.startswith("the broker")] == ["the broker refused publication 1"]
    _converges(ref.RETRY_DELIVERY, _history(ref.fail_attempt_atomically_then_notifying))


def test_a_refused_publication_skips_the_handoff_after_it() -> None:
    with pytest.raises(AssertionError) as divergence:
        _converges(ref.RETRY_DELIVERY, _history(ref.fail_attempt_publishing_after_a_notification))
    assert "'the broker refused publication 1': ('retryable_failed', ())" in str(divergence.value)


def test_robust_callbacks_protect_later_callbacks_but_not_their_own_handoff() -> None:
    with pytest.raises(AssertionError) as divergence:
        _converges(ref.RETRY_DELIVERY, _history(ref.fail_attempt_publishing_after_a_robust_audit))
    assert "'after-commit callback 1 failed'" not in str(divergence.value)
    assert "'after-commit callback 2 failed': ('retryable_failed', ())" in str(divergence.value)
