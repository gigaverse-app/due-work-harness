"""
The seams a history names: an external call counts when its effect has happened, and each declared seam must be used.

A call that returns an awaitable has not acted yet: the death must come after
the effect, or the history converges on a notification nobody sent. A seam on a
class keeps the binding Python gave it. And a declaration is held to the
transition per seam: one seam that is called cannot vouch for another that never is.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from due_work_harness.crash_histories import (
    ExternalCall,
    HandoffHistory,
    _worker,
    assert_crash_at_every_commit_converges,
)
from due_work_harness.host import Host, hosted
from due_work_harness.references import in_memory_handoffs as ref
from due_work_harness.worker_death import WorkerDied


def _notifying(transition: Callable[[int], Any], *seams: str) -> HandoffHistory[int, Any]:
    return HandoffHistory(
        name="completion",
        arrange=ref.running_attempt,
        transition=transition,
        observe=ref.status_and_notifications,
        external_calls=tuple(ExternalCall(ref.RECIPIENT, seam) for seam in seams),
    )


@pytest.mark.parametrize(
    ("transition", "seam"),
    [
        (ref.complete_notifying_async, "notify_async"),
        (ref.complete_notifying_deferred, "deferred_notify"),
    ],
    ids=["async-client", "deferred-result"],
)
def test_a_death_after_an_awaited_call_finds_the_repeat(
    ledger_host: Host, transition: Callable[[int], Any], seam: str
) -> None:
    # Killed before the coroutine ran, the recipient would have seen nothing and recovery's one
    # notification would look right. Killed after it, recovery's notification is the second.
    with pytest.raises(AssertionError, match=r"'worker died after external call 1': \('complete', 2\).*repeated"):
        assert_crash_at_every_commit_converges(ref.NOTIFYING_DELIVERY, _notifying(transition, seam))


def test_an_idempotent_async_notification_converges(ledger_host: Host) -> None:
    assert_crash_at_every_commit_converges(
        ref.NOTIFYING_ONCE_DELIVERY, _notifying(ref.complete_notifying_once_async, "notify_once_async")
    )


@pytest.mark.parametrize("with_called_seam", [False, True], ids=["only-unused", "called-and-unused"])
def test_every_declared_seam_must_be_called(ledger_host: Host, with_called_seam: bool) -> None:
    seams = ("notify", "notify_once") if with_called_seam else ("notify",)
    with pytest.raises(AssertionError, match=r"never called: Recipient\.notify\."):
        assert_crash_at_every_commit_converges(
            ref.NOTIFYING_ONCE_DELIVERY, _notifying(ref.complete_notifying_once, *seams)
        )


def _history(*calls: ExternalCall) -> HandoffHistory[int, Any]:
    return HandoffHistory(
        name="seams", arrange=lambda: 0, transition=lambda handle: None, observe=lambda handle: 0, external_calls=calls
    )


@pytest.mark.parametrize("method", ["instance_call", "static_call", "class_call"])
def test_class_seams_preserve_method_binding(method: str) -> None:
    received: list[int] = []

    class Provider:
        def instance_call(self, value: int) -> None:
            received.append(value)

        @staticmethod
        def static_call(value: int) -> None:
            received.append(value)

        @classmethod
        def class_call(cls, value: int) -> None:
            received.append(value)

    provider = Provider()
    with hosted(Host()), _worker(_history(ExternalCall(Provider, method)), None, None, None) as worker:
        getattr(provider, method)(42)
    assert received == [42]
    assert worker.calls == 1
    # Restoring the seam must restore the descriptor too, not a bound copy of it.
    getattr(provider, method)(43)
    assert received == [42, 43]
    assert isinstance(Provider.__dict__["static_call"], staticmethod)
    assert isinstance(Provider.__dict__["class_call"], classmethod)


@pytest.mark.parametrize("method", ["notify_async", "deferred_notify"])
def test_awaitable_results_are_completed_before_the_death(ledger_host: Host, method: str) -> None:
    async def invoke() -> None:
        await getattr(ref.RECIPIENT, method)(42)

    history = _history(ExternalCall(ref.RECIPIENT, method))
    with pytest.raises(WorkerDied), _worker(history, None, 1, None):
        asyncio.run(invoke())
    assert ref.RECIPIENT.received == [42]


def test_a_deferred_call_created_before_the_death_cannot_run_during_cleanup(ledger_host: Host) -> None:
    async def invoke() -> None:
        first = ref.RECIPIENT.deferred_notify(1)
        second = ref.RECIPIENT.deferred_notify(2)
        try:
            await first
        finally:
            await second

    history = _history(ExternalCall(ref.RECIPIENT, "deferred_notify"))
    with pytest.raises(WorkerDied), _worker(history, None, 1, None) as worker:
        asyncio.run(invoke())
    # The second effect belongs to a dead process: it never happens, and its coroutine is closed, not leaked.
    assert ref.RECIPIENT.received == [1]
    assert worker.calls == 1
