"""
The seams a history names: an external call counts when its effect has happened, and each declared seam must be used.

A call that returns an awaitable has not acted yet: the death must come after
the effect, or the history converges on a notification nobody sent. A seam on a
class keeps the binding Python gave it. And a declaration is held to the
transition per seam: one seam that is called cannot vouch for another that never is.
A seam whose result is not a coroutine (an async generator, a generator, a Task, an
awaitable object) is refused: it would have to be changed to be observed. So is a
seam declared twice, which would be wrapped twice.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest

from pytest_obligation.crash_histories import (
    ExternalCall,
    HandoffHistory,
    _worker,
    assert_crash_at_every_commit_converges,
)
from pytest_obligation.host import Host, hosted
from pytest_obligation.models import ObligationContractDesignError
from pytest_obligation.references import in_memory_handoffs as ref
from pytest_obligation.references.in_memory_handoffs import Recipient
from pytest_obligation.worker_death import WorkerDied


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


class _Provider:
    """
    Seams whose result is not a coroutine: the effect happens later, when the caller consumes it.

    Waiting for such a result would change what the caller receives (an ``async with``
    target, a Task with its callbacks, a generator to iterate), and not waiting would
    kill the worker before the effect. Neither is honest, so the seam is refused.
    """

    def __init__(self) -> None:
        self.received: list[int] = []

    async def stream(self, value: int) -> Any:
        self.received.append(value)  # The effect happens on iteration, like a streaming upload.
        yield value

    def chunks(self, value: int) -> Any:
        self.received.append(value)
        yield value

    def request(self, value: int) -> Any:
        return _Request(self, value)

    def schedule(self, value: int) -> "asyncio.Task[None]":
        return asyncio.get_running_loop().create_task(self.send(value))

    async def send(self, value: int) -> None:
        self.received.append(value)


class _Request:
    """An aiohttp-style request: awaitable, and an async context manager, but not a coroutine."""

    def __init__(self, provider: _Provider, value: int) -> None:
        self._provider, self._value = provider, value

    def __await__(self) -> Any:
        return self._provider.send(self._value).__await__()


@pytest.mark.parametrize(
    ("seam", "invoke", "kind"),
    [
        ("stream", lambda provider: _drain(provider.stream(1)), "an async generator"),
        ("chunks", lambda provider: _as_coroutine(list, provider.chunks(1)), "a generator"),
        ("request", lambda provider: provider.request(1), "an awaitable that is not a coroutine"),
        ("schedule", lambda provider: _awaited(provider.schedule, 1), "an awaitable that is not a coroutine"),
    ],
    ids=["async-generator", "generator", "custom-awaitable", "task"],
)
def test_a_seam_whose_result_is_not_a_coroutine_is_refused(seam: str, invoke: Callable[[Any], Any], kind: str) -> None:
    provider = _Provider()
    history = _history(ExternalCall(provider, seam))
    with (
        hosted(Host()),
        _worker(history, None, 1, None),
        pytest.raises(ObligationContractDesignError, match=rf"_Provider\.{seam} returned {kind}.*coroutine method"),
    ):
        asyncio.run(_run(invoke, provider))


def test_declaring_the_coroutine_underneath_an_awaitable_kills_after_its_effect() -> None:
    # The conforming declaration for the refused request above: the coroutine that performs the send.
    provider = _Provider()
    with hosted(Host()), pytest.raises(WorkerDied), _worker(_history(ExternalCall(provider, "send")), None, 1, None):
        asyncio.run(_run(lambda provider: provider.request(1), provider))
    assert provider.received == [1]


def test_a_seam_declared_twice_is_refused() -> None:
    # Declared twice, it would be wrapped twice and every call counted twice.
    with pytest.raises(ObligationContractDesignError, match=r"declares the external call Recipient\.notify twice"):
        _history(ExternalCall(ref.RECIPIENT, "notify"), ExternalCall(ref.RECIPIENT, "notify"))


async def _run(invoke: Callable[[Any], Any], provider: _Provider) -> None:
    result = invoke(provider)
    if asyncio.iscoroutine(result) or isinstance(result, _Request):
        await result


async def _drain(stream: Any) -> None:
    async for _ in stream:
        pass


async def _as_coroutine(consume: Callable[[Any], Any], value: Any) -> None:
    consume(value)


async def _awaited(schedule: Callable[[int], Any], value: int) -> None:
    await schedule(value)


def test_a_seam_returning_a_thread_pool_future_is_refused() -> None:
    # A concurrent.futures.Future is not awaitable, and its effect happens on the pool's thread, later.
    pool = ThreadPoolExecutor(max_workers=1)

    class Submitting:
        def submit(self, value: int) -> "Future[int]":
            return pool.submit(lambda: value)

    provider = Submitting()
    try:
        with (
            hosted(Host()),
            _worker(_history(ExternalCall(provider, "submit")), None, 1, None),
            pytest.raises(
                ObligationContractDesignError, match=r"Submitting\.submit returned a concurrent\.futures\.Future"
            ),
        ):
            provider.submit(1)
    finally:
        pool.shutdown()


def test_a_refusal_the_transition_swallows_is_still_reported(ledger_host: Host) -> None:
    # Production's best-effort ``except Exception`` catches the refusal; the history must still report it,
    # rather than fail later as though the seam had never been called.
    history = _notifying(ref.complete_notifying_best_effort, "notify_in_chunks", "notify")
    with pytest.raises(ObligationContractDesignError, match=r"Recipient\.notify_in_chunks returned a generator"):
        assert_crash_at_every_commit_converges(ref.NOTIFYING_DELIVERY, history)


class _Base:
    def send(self, value: int) -> None:
        pass


class _Sub(_Base):
    pass


class _Overriding(_Base):
    def send(self, value: int) -> None:
        pass


@pytest.mark.parametrize(
    "owners",
    [(Recipient, "instance"), (_Base, _Sub), (_Base, "sub-instance"), ("sub-instance", _Sub)],
    ids=["class-and-its-instance", "class-and-subclass", "class-and-a-subclass-instance", "instance-and-its-class"],
)
def test_one_function_declared_through_overlapping_owners_is_refused(owners: tuple[object, object]) -> None:
    # Declared through two owners that share one function, it would be wrapped twice and counted twice.
    instances = {"instance": ref.RECIPIENT, "sub-instance": _Sub()}
    first, second = (instances.get(owner, owner) if isinstance(owner, str) else owner for owner in owners)
    attribute = "notify" if first is Recipient else "send"
    with pytest.raises(ObligationContractDesignError, match="declares the external call .* twice"):
        _history(ExternalCall(first, attribute), ExternalCall(second, attribute))


@pytest.mark.parametrize(
    "owners",
    [("two-instances",), ("overriding-subclass",)],
    ids=["two-instances-of-one-class", "a-subclass-that-overrides"],
)
def test_distinct_seams_are_not_duplicates(owners: tuple[str]) -> None:
    if owners == ("two-instances",):
        _history(ExternalCall(Recipient(), "notify"), ExternalCall(Recipient(), "notify"))
    else:
        _history(ExternalCall(_Base, "send"), ExternalCall(_Overriding, "send"))


def test_an_instance_seam_is_removed_again_not_left_as_a_bound_method() -> None:
    # Restored by assignment, an instance seam would leave a bound method in the instance's own namespace,
    # where it shadows every later patch of the class: a class seam would then never see this instance.
    recipient = Recipient()
    with hosted(Host()), _worker(_history(ExternalCall(recipient, "notify")), None, None, None):
        recipient.notify(1)
    assert "notify" not in vars(recipient)
    with hosted(Host()), _worker(_history(ExternalCall(Recipient, "notify")), None, None, None) as worker:
        recipient.notify(2)
    assert worker.calls == 1
    assert "notify" in vars(Recipient) and not isinstance(vars(Recipient)["notify"], type(recipient.notify))


def test_a_refusal_the_transition_wraps_is_reported_as_itself(ledger_host: Host) -> None:
    # Production re-raises its client's own error ``from`` the refusal; the refusal is what the history reports.
    history = _notifying(ref.complete_notifying_wrapping_errors, "notify_in_chunks")
    with pytest.raises(ObligationContractDesignError, match=r"Recipient\.notify_in_chunks returned a generator"):
        assert_crash_at_every_commit_converges(ref.NOTIFYING_DELIVERY, history)


class _Slotted:
    __slots__ = ("send",)

    def __init__(self) -> None:
        self.send = lambda: "sent"


def test_a_seam_held_in_a_slot_is_put_back_not_deleted() -> None:
    provider = _Slotted()
    with hosted(Host()), _worker(_history(ExternalCall(provider, "send")), None, None, None) as worker:
        assert provider.send() == "sent"
    assert worker.calls == 1
    assert provider.send() == "sent"
