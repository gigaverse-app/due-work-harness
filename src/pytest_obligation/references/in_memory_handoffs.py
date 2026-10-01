"""
Framework-free references for crash histories: a ledger whose commits can be interrupted.

:class:`Ledger` is a deliberately small database: rows keyed by id, writes that
commit on their own, ``atomic()`` blocks that commit once at the end,
after-commit callbacks, and an outbox of published messages. :func:`ledger_killer`
is its :class:`~pytest_obligation.host.WorkerKiller`: it counts the ledger's
commits and kills the worker right after the chosen one — dropping pending
after-commit callbacks and refusing every later write, as a dead process's
connection would. :func:`ledger_reply_breaker` is its
:class:`~pytest_obligation.host.ReplyBreaker`: the chosen commit lands, then
:class:`LedgerConnectionError` reaches the caller, as a dropped connection's
would.

The attempt lifecycle below reduces a retrying pipeline to its handoff shapes:
a failure and the successor it owes committed together, split across two
commits, handed off only as a message, handed off from a commit hook; and a
completion that notifies an external recipient, with and without an
idempotency key. The harness's own self-tests run every crash history against
them, in both directions. Never bind these in an adopter.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from typing import Any

import pydantic

from pytest_obligation.crash_histories import CallableDelivery
from pytest_obligation.models import MutableHarnessModel
from pytest_obligation.worker_death import WorkerDied


class _Death(MutableHarnessModel):
    commits: int = 0
    dead: bool = False
    kill_after: int | None = None

    def kill_now(self, reason: str) -> None:
        self.dead = True
        raise WorkerDied(reason)


class LedgerConnectionError(Exception):
    """The ledger's connection dropped after a write landed, before its answer arrived."""


class _LostReplies:
    """The ledger's commits as a reply breaker counts them (a ``CountedFaults``)."""

    def __init__(self, lose_at: int | None) -> None:
        self._lose_at = lose_at
        self.count = 0
        self.failure: LedgerConnectionError | None = None

    def committed(self) -> None:
        self.count += 1
        if self.count == self._lose_at:
            self.failure = LedgerConnectionError(f"the reply to commit {self.count} was lost; the write landed")
            raise self.failure


class Ledger(MutableHarnessModel):
    rows: dict[int, dict[str, Any]] = pydantic.Field(default_factory=dict)
    outbox: list[tuple[str, int]] = pydantic.Field(default_factory=list)
    _next_id: int = pydantic.PrivateAttr(default=1)
    _pending: list[tuple[int, dict[str, Any]]] | None = pydantic.PrivateAttr(default=None)
    _after_commit: list[Callable[[], None]] = pydantic.PrivateAttr(default_factory=list)
    _worker: _Death | None = pydantic.PrivateAttr(default=None)
    _replies: _LostReplies | None = pydantic.PrivateAttr(default=None)

    def insert(self, **values: Any) -> int:
        row_id = self._next_id
        self._next_id += 1
        self._write(row_id, values)
        return row_id

    def update(self, row_id: int, **values: Any) -> None:
        self._write(row_id, values)

    def get(self, row_id: int) -> dict[str, Any]:
        return dict(self.rows[row_id])

    def select(self, **where: Any) -> list[int]:
        return sorted(key for key, row in self.rows.items() if all(row.get(k) == v for k, v in where.items()))

    def on_commit(self, callback: Callable[[], None]) -> None:
        if self._pending is None:
            callback()
        else:
            self._after_commit.append(callback)

    def publish(self, task: str, row_id: int) -> None:
        self.on_commit(lambda: self.outbox.append((task, row_id)))

    @contextmanager
    def session(self) -> Iterator[None]:
        """A client session whose close fails when an error is on its way out, replacing that error."""
        try:
            yield
        except BaseException as error:  # noqa: BLE001 - the flawed cleanup under test
            raise LedgerConnectionError("the session could not be closed after an error") from error

    @contextmanager
    def atomic(self) -> Iterator[None]:
        self._refuse_if_dead()
        assert self._pending is None, "the reference ledger has no nested transactions"
        self._pending = []
        try:
            yield
        except BaseException:
            self._pending, self._after_commit = None, []
            raise
        pending, callbacks = self._pending, self._after_commit
        self._pending, self._after_commit = None, []
        self._apply(pending)
        self._committed()
        for callback in callbacks:
            callback()

    def _write(self, row_id: int, values: dict[str, Any]) -> None:
        self._refuse_if_dead()
        if self._pending is not None:
            self._pending.append((row_id, values))
            return
        self._apply([(row_id, values)])
        self._committed()

    def _apply(self, writes: list[tuple[int, dict[str, Any]]]) -> None:
        for row_id, values in writes:
            self.rows.setdefault(row_id, {}).update(values)

    def _committed(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.commits += 1
            if worker.commits == worker.kill_after:
                self._after_commit.clear()
                worker.kill_now(f"worker died right after commit {worker.commits}")
        if self._replies is not None:
            self._replies.committed()

    def _refuse_if_dead(self) -> None:
        if self._worker is not None and self._worker.dead:
            raise WorkerDied("the worker is dead; its connection runs nothing more")


LEDGER = Ledger()


@contextmanager
def ledger_killer(kill_after: int | None) -> Iterator[_Death]:
    """The ledger's WorkerKiller: count commits, die right after commit ``kill_after``."""
    worker = _Death(kill_after=kill_after)
    LEDGER._worker = worker
    try:
        yield worker
    finally:
        LEDGER._worker = None
        LEDGER._pending, LEDGER._after_commit = None, []


@contextmanager
def ledger_reply_breaker(lose_at: int | None) -> Iterator[_LostReplies]:
    """The ledger's ReplyBreaker: count commits, lose the reply to commit ``lose_at``."""
    replies = _LostReplies(lose_at)
    LEDGER._replies = replies
    try:
        yield replies
    finally:
        LEDGER._replies = None


# --- The reference attempt lifecycle -------------------------------------------------------

REQUESTED, RUNNING, RETRYABLE_FAILED, COMPLETE = "requested", "running", "retryable_failed", "complete"


def running_attempt() -> int:
    return LEDGER.insert(status=RUNNING, retry_of=None)


def attempt_and_successors(attempt: int) -> tuple[str, tuple[str, ...]]:
    successors = LEDGER.select(retry_of=attempt)
    return LEDGER.get(attempt)["status"], tuple(LEDGER.get(successor)["status"] for successor in successors)


def _create_successor(attempt: int) -> None:
    if not LEDGER.select(retry_of=attempt):
        LEDGER.insert(status=REQUESTED, retry_of=attempt)


def fail_with_atomic_handoff(attempt: int) -> None:
    """The conforming shape: the failure and the successor it owes commit together."""
    with LEDGER.atomic():
        LEDGER.update(attempt, status=RETRYABLE_FAILED)
        _create_successor(attempt)


def fail_with_atomic_handoff_checking_in_cleanup(attempt: int) -> None:
    """The atomic handoff, with a cleanup that asserts it finished: a dead process never runs that check."""
    finished = False
    try:
        fail_with_atomic_handoff(attempt)
        finished = True
    finally:
        assert finished, "the handoff did not finish"


def fail_with_atomic_handoff_in_a_task_group(attempt: int) -> None:
    """The atomic handoff run as a task: a death inside it reaches the caller as a group."""

    async def fail() -> None:
        fail_with_atomic_handoff(attempt)

    async def run() -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(fail())

    asyncio.run(run())


def fail_with_atomic_handoff_then_a_group_with_its_own_death(attempt: int) -> None:
    """A live worker raising a death of its own in a group: no simulated death, so nothing to absorb."""
    fail_with_atomic_handoff(attempt)
    raise BaseExceptionGroup("tasks", [WorkerDied("not the simulated death")])


def fail_with_split_handoff(attempt: int) -> None:
    """The failure commits, then the successor commits separately."""
    LEDGER.update(attempt, status=RETRYABLE_FAILED)
    _create_successor(attempt)


def fail_with_split_handoff_in_a_session(attempt: int) -> None:
    """The split handoff inside a session whose close raises its own error after the worker dies."""
    with LEDGER.session():
        fail_with_split_handoff(attempt)


def fail_with_message_handoff(attempt: int) -> None:
    """The failure commits; the retry exists only as a published message."""
    with LEDGER.atomic():
        LEDGER.update(attempt, status=RETRYABLE_FAILED)
        LEDGER.publish("reconcile", attempt)


def fail_with_commit_hook_handoff(attempt: int) -> None:
    """The failure commits; an after-commit hook creates the successor."""
    with LEDGER.atomic():
        LEDGER.update(attempt, status=RETRYABLE_FAILED)
        LEDGER.on_commit(lambda: _create_successor(attempt))


def reconcile(attempt: int) -> None:
    """The per-attempt worker: advance requested work, hand off a retry still owed."""
    status = LEDGER.get(attempt)["status"]
    if status == REQUESTED:
        LEDGER.update(attempt, status=RUNNING)
    elif status == RETRYABLE_FAILED:
        _create_successor(attempt)


def run_tick() -> int:
    """Recovery: reconcile every requested attempt, as a scheduled sweep would."""
    due = LEDGER.select(status=REQUESTED)
    for attempt in due:
        reconcile(attempt)
    return len(due)


def run_inert_tick() -> int:
    """A recovery that recovers nothing."""
    return 0


def drain_outbox() -> None:
    while LEDGER.outbox:
        _task, attempt = LEDGER.outbox.pop(0)
        reconcile(attempt)


def recover() -> None:
    for _round in range(3):
        drain_outbox()
        run_tick()
        drain_outbox()


def recover_nothing() -> None:
    drain_outbox()
    run_inert_tick()


RETRY_DELIVERY = CallableDelivery(
    name="reference retries", recover=recover, deliver=drain_outbox, lose=LEDGER.outbox.clear
)
INERT_DELIVERY = CallableDelivery(
    name="inert recovery", recover=recover_nothing, deliver=drain_outbox, lose=LEDGER.outbox.clear
)


# --- Notifications: an external call between the work and its record ------------------------------


class Recipient:
    """
    An external system that records every notification it receives.

    A plain class, not a harness model: it stands in for a provider client,
    and crash histories patch its methods on the instance at the
    ``ExternalCall`` seam, which a Pydantic model refuses as an unknown field.
    """

    def __init__(self) -> None:
        self.received: list[int] = []

    def notify(self, attempt: int) -> None:
        self.received.append(attempt)

    def notify_once(self, attempt: int) -> None:
        """The same call keyed by the attempt: the recipient drops a repeated key."""
        if attempt not in self.received:
            self.received.append(attempt)

    async def notify_async(self, attempt: int) -> None:
        """An async client records the effect when awaited, not when called."""
        self.notify(attempt)

    async def notify_once_async(self, attempt: int) -> None:
        """The keyed call on an async client."""
        self.notify_once(attempt)

    def deferred_notify(self, attempt: int) -> Coroutine[Any, Any, None]:
        """An SDK-style sync method that returns deferred work rather than its result."""
        return self.notify_async(attempt)

    def notify_in_chunks(self, attempt: int) -> Iterator[int]:
        """A streaming client: the notification leaves as the caller iterates."""
        self.received.append(attempt)
        yield attempt


RECIPIENT = Recipient()


def complete_notifying(attempt: int) -> None:
    RECIPIENT.notify(attempt)
    LEDGER.update(attempt, status=COMPLETE)


def complete_notifying_once(attempt: int) -> None:
    RECIPIENT.notify_once(attempt)
    LEDGER.update(attempt, status=COMPLETE)


def complete_notifying_async(attempt: int) -> None:
    """Notify through an async client, then record completion."""
    asyncio.run(RECIPIENT.notify_async(attempt))
    LEDGER.update(attempt, status=COMPLETE)


def complete_notifying_deferred(attempt: int) -> None:
    """Notify through a sync method that returns the deferred effect, then record completion."""
    asyncio.run(RECIPIENT.deferred_notify(attempt))
    LEDGER.update(attempt, status=COMPLETE)


def complete_notifying_once_async(attempt: int) -> None:
    """The keyed notification on an async client."""
    asyncio.run(RECIPIENT.notify_once_async(attempt))
    LEDGER.update(attempt, status=COMPLETE)


def complete_notifying_best_effort(attempt: int) -> None:
    """Completion, a streamed notification whose errors are swallowed as best effort, then a plain one."""
    LEDGER.update(attempt, status=COMPLETE)
    try:
        list(RECIPIENT.notify_in_chunks(attempt))
    except Exception:  # noqa: BLE001, S110 - the production shape under test: a best-effort call
        pass
    RECIPIENT.notify(attempt)


def complete_notifying_wrapping_errors(attempt: int) -> None:
    """Completion, then a streamed notification whose errors are wrapped in the client's own error type."""
    LEDGER.update(attempt, status=COMPLETE)
    try:
        list(RECIPIENT.notify_in_chunks(attempt))
    except Exception as error:
        raise LedgerConnectionError("the notification could not be sent") from error


def status_and_notifications(attempt: int) -> tuple[str, int]:
    return LEDGER.get(attempt)["status"], RECIPIENT.received.count(attempt)


def recover_running_notifying() -> None:
    for attempt in LEDGER.select(status=RUNNING):
        complete_notifying(attempt)


def recover_running_notifying_once() -> None:
    for attempt in LEDGER.select(status=RUNNING):
        complete_notifying_once(attempt)


NOTIFYING_DELIVERY = CallableDelivery(name="notifications", recover=recover_running_notifying)
NOTIFYING_ONCE_DELIVERY = CallableDelivery(name="keyed notifications", recover=recover_running_notifying_once)


# --- Completion under a lost reply: what the worker does when a write's answer never arrives -------

FAILED = "failed"


def complete_failing_on_error(attempt: int) -> None:
    """Records its progress, then completion, and treats an error from that write as a failed attempt."""
    LEDGER.update(attempt, progress="done")
    try:
        LEDGER.update(attempt, status=COMPLETE)
    except LedgerConnectionError:
        LEDGER.update(attempt, status=FAILED)


def complete_asserting_on_error(attempt: int) -> None:
    """Records its progress, then completion, and treats an error from that write as a broken invariant."""
    LEDGER.update(attempt, progress="done")
    try:
        LEDGER.update(attempt, status=COMPLETE)
    except LedgerConnectionError as error:
        raise AssertionError("the completion's reply must never be lost") from error


def complete_grouping_its_assertion_on_error(attempt: int) -> None:
    """The broken invariant again, reported inside an exception group, as a task group or except* would."""
    LEDGER.update(attempt, progress="done")
    try:
        LEDGER.update(attempt, status=COMPLETE)
    except LedgerConnectionError as error:
        raise ExceptionGroup(
            "completing the attempt", [AssertionError("the completion's reply must never be lost")]
        ) from error


def complete_checking_on_error(attempt: int) -> None:
    """The conforming shape: after an error, it reads back what landed before deciding."""
    LEDGER.update(attempt, progress="done")
    try:
        LEDGER.update(attempt, status=COMPLETE)
    except LedgerConnectionError:
        if LEDGER.get(attempt)["status"] != COMPLETE:
            LEDGER.update(attempt, status=FAILED)


class CompletionUnconfirmed(Exception):  # noqa: N818 - named for what the worker knows
    """The application's own error for a completion whose reply never arrived."""


def complete_in_a_task_group_checking_on_error(attempt: int) -> None:
    """Completes as a task: the lost reply reaches the caller inside the task group's ExceptionGroup."""
    LEDGER.update(attempt, progress="done")

    async def complete() -> None:
        LEDGER.update(attempt, status=COMPLETE)

    async def run() -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(complete())

    asyncio.run(run())


def complete_translating_a_lost_reply(attempt: int) -> None:
    """Replaces the driver's error with its own, deliberately (``from None``)."""
    LEDGER.update(attempt, progress="done")
    try:
        LEDGER.update(attempt, status=COMPLETE)
    except LedgerConnectionError:
        raise CompletionUnconfirmed("the completion's reply never arrived") from None


def complete_raising_unchained_on_error(attempt: int) -> None:
    """A handler bug: a new error raised while handling the lost reply, chained only implicitly."""
    LEDGER.update(attempt, progress="done")
    try:
        LEDGER.update(attempt, status=COMPLETE)
    except LedgerConnectionError:
        raise CompletionUnconfirmed("the completion's reply never arrived")  # noqa: B904 - the bug under test


def complete_raising_unlinked_after_error(attempt: int) -> None:
    """Raises after the lost reply was handled, linked to it by nothing."""
    LEDGER.update(attempt, progress="done")
    lost = False
    try:
        LEDGER.update(attempt, status=COMPLETE)
    except LedgerConnectionError:
        lost = True
    if lost:
        raise CompletionUnconfirmed("the completion's reply never arrived")


def attempt_status(attempt: int) -> str:
    return LEDGER.get(attempt)["status"]


def recover_running_completions() -> None:
    for attempt in LEDGER.select(status=RUNNING):
        complete_checking_on_error(attempt)


COMPLETION_DELIVERY = CallableDelivery(name="completions", recover=recover_running_completions)


def reset() -> None:
    LEDGER.rows.clear()
    LEDGER.outbox.clear()
    LEDGER._next_id = 1
    RECIPIENT.received.clear()


def recover_after_death(attempt: int) -> None:
    """A restart's recovery for one attempt: finish running work, notifying as normal operation does."""
    if LEDGER.get(attempt)["status"] == RUNNING:
        complete_notifying(attempt)
