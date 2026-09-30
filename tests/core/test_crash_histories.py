"""
Crash histories against the framework-free reference ledger, in both directions.

The conforming shapes commit a failure together with the successor it owes, or
notify with an idempotency key; they converge. Each broken shape diverges on
its own history, named in the failure: a split commit, a message-only handoff,
a commit hook, a repeated notification. Agreement alone is not a pass: an inert
recovery fails the positive control.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from due_work_harness.crash_histories import (
    CallableDelivery,
    ExternalCall,
    HandoffHistory,
    HistoriesDiverged,
    assert_crash_at_every_commit_converges,
    assert_pinned_outcomes,
)
from due_work_harness.host import Host, hosted
from due_work_harness.references import in_memory_handoffs as ref


def _retry(transition: Callable[[int], Any]) -> HandoffHistory[int, Any]:
    return HandoffHistory(
        name="retryable failure",
        arrange=ref.running_attempt,
        transition=transition,
        observe=ref.attempt_and_successors,
    )


def test_an_atomic_handoff_converges(ledger_host: Host) -> None:
    assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, _retry(ref.fail_with_atomic_handoff))


@pytest.mark.parametrize(
    ("transition", "divergence"),
    [
        (ref.fail_with_split_handoff, r"'worker died after commit 1': \('retryable_failed', \(\)\)"),
        (ref.fail_with_message_handoff, r"'notifications lost': \('retryable_failed', \(\)\)"),
        (ref.fail_with_commit_hook_handoff, r"'worker died after commit 1': \('retryable_failed', \(\)\)"),
    ],
    ids=["split-commits", "message-only", "commit-hook"],
)
def test_a_handoff_a_death_or_lost_message_strands_diverges(
    ledger_host: Host, transition: Callable[[int], Any], divergence: str
) -> None:
    with pytest.raises(AssertionError, match=divergence):
        assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, _retry(transition))


def test_agreement_with_an_inert_recovery_fails_the_positive_control(ledger_host: Host) -> None:
    # Nothing ever advances the successor, so every history agrees on unfinished work.
    with pytest.raises(AssertionError, match="recovery changed the observation in none of them"):
        assert_crash_at_every_commit_converges(ref.INERT_DELIVERY, _retry(ref.fail_with_atomic_handoff))


def _notifying(transition: Callable[[int], Any], seam: str) -> HandoffHistory[int, Any]:
    return HandoffHistory(
        name="completion",
        arrange=ref.running_attempt,
        transition=transition,
        observe=ref.status_and_notifications,
        external_calls=(ExternalCall(ref.RECIPIENT, seam),),
    )


def test_a_notification_recovery_repeats_diverges(ledger_host: Host) -> None:
    with pytest.raises(AssertionError, match=r"'worker died after external call 1': \('complete', 2\).*repeated"):
        assert_crash_at_every_commit_converges(ref.NOTIFYING_DELIVERY, _notifying(ref.complete_notifying, "notify"))


def test_an_idempotent_notification_converges(ledger_host: Host) -> None:
    assert_crash_at_every_commit_converges(
        ref.NOTIFYING_ONCE_DELIVERY, _notifying(ref.complete_notifying_once, "notify_once")
    )


def test_naming_a_seam_the_transition_never_calls_is_refused(ledger_host: Host) -> None:
    with pytest.raises(AssertionError, match=r"never called: Recipient\.notify\."):
        assert_crash_at_every_commit_converges(
            ref.NOTIFYING_ONCE_DELIVERY, _notifying(ref.complete_notifying_once, "notify")
        )


def test_a_transition_that_changes_nothing_fails_the_positive_control(ledger_host: Host) -> None:
    history = HandoffHistory(
        name="heartbeat", arrange=ref.running_attempt, transition=ref.reconcile, observe=ref.attempt_and_successors
    )
    with pytest.raises(
        AssertionError, match="positive control failed: normal operation left the observation unchanged"
    ):
        assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, history)


def test_a_test_authored_transition_is_refused(ledger_host: Host) -> None:
    def authored(attempt: int) -> None:
        ref.LEDGER.update(attempt, status=ref.RETRYABLE_FAILED)

    with pytest.raises(AssertionError, match=r"handoff\[retryable failure\]\.transition authors production semantics"):
        assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, _retry(authored))


def test_a_test_authored_recovery_is_refused(ledger_host: Host) -> None:
    def authored_recovery() -> None:
        for attempt in ref.LEDGER.select(status=ref.REQUESTED):
            ref.LEDGER.update(attempt, status=ref.RUNNING)

    delivery = CallableDelivery(name="authored", recover=authored_recovery)
    with pytest.raises(
        AssertionError, match=r"recover (authors production semantics|is test code that references no production)"
    ):
        assert_crash_at_every_commit_converges(delivery, _retry(ref.fail_with_atomic_handoff))


def test_crash_histories_need_a_worker_killer() -> None:
    with hosted(Host()), pytest.raises(AssertionError, match="worker_killer"):
        assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, _retry(ref.fail_with_atomic_handoff))


def _completion(transition: Callable[[int], Any]) -> HandoffHistory[int, Any]:
    return HandoffHistory(
        name="completion", arrange=ref.running_attempt, transition=transition, observe=ref.attempt_status
    )


@pytest.fixture
def replying_ledger_host() -> Iterator[Host]:
    """The ledger host, also losing the reply to each commit in turn."""
    ref.reset()
    with hosted(Host(worker_killer=ref.ledger_killer, reply_breaker=ref.ledger_reply_breaker)) as host:
        yield host
    ref.reset()


def test_reading_back_after_a_lost_reply_converges(replying_ledger_host: Host) -> None:
    assert_crash_at_every_commit_converges(ref.COMPLETION_DELIVERY, _completion(ref.complete_checking_on_error))


def test_treating_a_lost_reply_as_a_failure_diverges(replying_ledger_host: Host) -> None:
    # The completion landed; the worker never saw the answer and recorded a failure over it.
    with pytest.raises(AssertionError, match=r"'the reply to commit 2 was lost': 'failed'"):
        assert_crash_at_every_commit_converges(ref.COMPLETION_DELIVERY, _completion(ref.complete_failing_on_error))


def test_a_findings_table_lists_only_the_findings(replying_ledger_host: Host) -> None:
    history = _completion(ref.complete_failing_on_error)
    assert_pinned_outcomes(
        ref.COMPLETION_DELIVERY,
        history,
        delivered=ref.COMPLETE,
        outcomes={"the reply to commit 2 was lost": ref.FAILED},
    )
    # A finding that no longer happens is reported, and so is one pinned for a history that never ran.
    with pytest.raises(AssertionError, match=r"the reply to commit 2 was lost: pinned normal operation"):
        assert_pinned_outcomes(ref.COMPLETION_DELIVERY, history, delivered=ref.COMPLETE, outcomes={})
    with pytest.raises(AssertionError, match=r"worker died after commit 7: pinned 'failed', now '<not run>'"):
        assert_pinned_outcomes(
            ref.COMPLETION_DELIVERY,
            history,
            delivered=ref.COMPLETE,
            outcomes={"the reply to commit 2 was lost": ref.FAILED, "worker died after commit 7": ref.FAILED},
        )


def test_an_assertion_raised_after_an_injected_failure_is_never_absorbed(replying_ledger_host: Host) -> None:
    # The lost reply is injected, and production answers it with a broken invariant: that must fail the
    # history, not pass as the injected failure reaching the caller.
    with pytest.raises(AssertionError, match="the completion's reply must never be lost"):
        assert_crash_at_every_commit_converges(ref.COMPLETION_DELIVERY, _completion(ref.complete_asserting_on_error))


def test_an_ordinary_error_raised_on_the_way_out_of_a_dead_worker_is_absorbed(ledger_host: Host) -> None:
    # A session's close raises its own error over the death; a dead process runs no close at all, so the
    # history goes on to its verdict instead of erroring.
    with pytest.raises(AssertionError, match=r"'worker died after commit 1': \('retryable_failed', \(\)\)"):
        assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, _retry(ref.fail_with_split_handoff_in_a_session))


def test_a_transition_that_changes_nothing_fails_the_positive_control_not_as_a_divergence(ledger_host: Host) -> None:
    history = HandoffHistory(
        name="heartbeat", arrange=ref.running_attempt, transition=ref.reconcile, observe=ref.attempt_and_successors
    )
    with pytest.raises(AssertionError, match="positive control failed") as raised:
        assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, history)
    assert not isinstance(raised.value, HistoriesDiverged)


def test_a_divergence_is_always_a_histories_diverged(ledger_host: Host) -> None:
    with pytest.raises(HistoriesDiverged, match=r"'worker died after commit 1'"):
        assert_crash_at_every_commit_converges(ref.RETRY_DELIVERY, _retry(ref.fail_with_split_handoff))
