"""
Framework-free references for crash histories: a ledger whose commits can be interrupted.

:class:`Ledger` is a deliberately small database: rows keyed by id, writes that
commit on their own, ``atomic()`` blocks that commit once at the end,
after-commit callbacks, and an outbox of published messages. :func:`ledger_killer`
is its :class:`~due_work_harness.host.WorkerKiller`: it counts the ledger's
commits and kills the worker right after the chosen one — dropping pending
after-commit callbacks and refusing every later write, as a dead process's
connection would.

The attempt lifecycle below reduces a retrying pipeline to its handoff shapes:
a failure and the successor it owes committed together, split across two
commits, handed off only as a message, handed off from a commit hook; and a
completion that notifies an external recipient, with and without an
idempotency key. The harness's own self-tests run every crash history against
them, in both directions. Never bind these in an adopter.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from due_work_harness.crash_histories import CallableDelivery
from due_work_harness.worker_death import WorkerDied


@dataclass
class _Death:
    commits: int = 0
    dead: bool = False
    kill_after: int | None = None

    def kill_now(self, reason: str) -> None:
        self.dead = True
        raise WorkerDied(reason)


@dataclass
class Ledger:
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)
    outbox: list[tuple[str, int]] = field(default_factory=list)
    _next_id: int = 1
    _pending: list[tuple[int, dict[str, Any]]] | None = None
    _after_commit: list[Callable[[], None]] = field(default_factory=list)
    _worker: _Death | None = None

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
        if worker is None:
            return
        worker.commits += 1
        if worker.commits == worker.kill_after:
            self._after_commit.clear()
            worker.kill_now(f"worker died right after commit {worker.commits}")

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


def fail_with_split_handoff(attempt: int) -> None:
    """The failure commits, then the successor commits separately."""
    LEDGER.update(attempt, status=RETRYABLE_FAILED)
    _create_successor(attempt)


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


@dataclass
class Recipient:
    """An external system that records every notification it receives."""

    received: list[int] = field(default_factory=list)

    def notify(self, attempt: int) -> None:
        self.received.append(attempt)

    def notify_once(self, attempt: int) -> None:
        """The same call keyed by the attempt: the recipient drops a repeated key."""
        if attempt not in self.received:
            self.received.append(attempt)


RECIPIENT = Recipient()


def complete_notifying(attempt: int) -> None:
    RECIPIENT.notify(attempt)
    LEDGER.update(attempt, status=COMPLETE)


def complete_notifying_once(attempt: int) -> None:
    RECIPIENT.notify_once(attempt)
    LEDGER.update(attempt, status=COMPLETE)


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


def reset() -> None:
    LEDGER.rows.clear()
    LEDGER.outbox.clear()
    LEDGER._next_id = 1
    RECIPIENT.received.clear()


def recover_after_death(attempt: int) -> None:
    """A restart's recovery for one attempt: finish running work, notifying as normal operation does."""
    if LEDGER.get(attempt)["status"] == RUNNING:
        complete_notifying(attempt)
