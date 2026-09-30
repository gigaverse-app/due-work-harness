"""
A reference attempt lifecycle on a real PostgreSQL table, for the harness's own self-tests.

One test-owned table (:class:`LifecycleAttempt`, created and dropped by
:func:`lifecycle_attempt_table`) and the handoff shapes a retrying pipeline can
take: a failure committed together with the successor it owes, split across
two commits, written by a SQL function in autocommit, deferred to a commit
hook or a ``finally`` block, handed off only as a message; and a completion
that notifies an external recipient, with and without an idempotency key.

Published work goes to :data:`OUTBOX`, an in-memory queue that
:data:`RETRY_DELIVERY` drains; recovery runs the reference tick with the clock
frozen past the recovery delay. Work published through Celery's ``send_task``
is only ever observed (by a publication recorder), never delivered.

Never import this module in an adopter: binding it measures the reference.
"""

import asyncio
import functools
import gc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from django.db import connection, models, transaction
from django.db.models import QuerySet
from django.dispatch import Signal
from django.test.utils import isolate_apps
from django.utils import timezone

from due_work_harness.crash_histories import CallableDelivery
from due_work_harness.host import current_host
from due_work_harness.references.in_memory_handoffs import Recipient

with isolate_apps("due_work_harness"):

    class LifecycleAttempt(models.Model):
        class Status(models.TextChoices):
            REQUESTED = "requested"
            RUNNING = "running"
            RETRYABLE_FAILED = "retryable_failed"
            TERMINAL_FAILED = "terminal_failed"
            COMPLETE = "complete"

        status = models.CharField(max_length=32, choices=Status.choices)
        retry_of = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="successors")
        updated_at = models.DateTimeField(default=timezone.now)
        reconciliation_claimed_at = models.DateTimeField(null=True)

        class Meta:
            app_label = "due_work_harness"


Status = LifecycleAttempt.Status
ACTIVE_STATUSES = (Status.REQUESTED, Status.RUNNING)

#: A SQL function that fails an attempt: called by ``SELECT``, its write reports ``SELECT 1``.
FAIL_ATTEMPT_FUNCTION = "due_work_harness_fail_attempt"

#: Attempts touched more recently than this are left to the worker that has them.
RECOVERY_DELAY = timedelta(minutes=1)

#: Published work, as (task, attempt id) pairs, until a delivery drains it.
OUTBOX: list[tuple[str, int]] = []


@contextmanager
def lifecycle_attempt_table() -> Iterator[None]:
    """Create the reference table and function for one test; drop them afterwards."""
    with connection.schema_editor() as editor:
        editor.create_model(LifecycleAttempt)
    table = LifecycleAttempt._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"CREATE FUNCTION {FAIL_ATTEMPT_FUNCTION}(attempt_id bigint) RETURNS void LANGUAGE sql AS "
            f"$$ UPDATE {table} SET status = '{Status.RETRYABLE_FAILED}' WHERE id = attempt_id $$"
        )
    try:
        yield
    finally:
        OUTBOX.clear()
        RECIPIENT.received.clear()
        with connection.cursor() as cursor:
            cursor.execute(f"DROP FUNCTION IF EXISTS {FAIL_ATTEMPT_FUNCTION}(bigint)")
        with connection.schema_editor() as editor:
            editor.delete_model(LifecycleAttempt)


def due_for_recovery() -> QuerySet[LifecycleAttempt]:
    """Active attempts untouched for the recovery delay, oldest first."""
    return LifecycleAttempt.objects.filter(
        status__in=ACTIVE_STATUSES, updated_at__lte=timezone.now() - RECOVERY_DELAY
    ).order_by("updated_at", "pk")


def publish(task: str, pk: int) -> None:
    OUTBOX.append((task, pk))


def run_recovery_tick() -> int:
    """Publish the reference worker for every due attempt, as a real tick would."""
    due = [attempt.pk for attempt in due_for_recovery()]
    for pk in due:
        publish("reconcile", pk)
    return len(due)


def run_inert_recovery_tick() -> int:
    """A tick that selects and publishes nothing: recovery that recovers no work."""
    return 0


def _create_successor(predecessor: LifecycleAttempt) -> None:
    if not LifecycleAttempt.objects.filter(retry_of=predecessor).exists():
        LifecycleAttempt.objects.create(status=Status.REQUESTED, retry_of=predecessor)


@transaction.atomic
def classify_retryable_failure_atomically(pk: int) -> None:
    """Classification and the successor it owes commit together."""
    attempt = LifecycleAttempt.objects.select_for_update().get(pk=pk)
    attempt.status = Status.RETRYABLE_FAILED
    attempt.save(update_fields=["status"])
    _create_successor(attempt)


@transaction.atomic
def classify_retryable_failure(pk: int) -> None:
    """Classification only; the reconcile worker hands off the successor later."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)


def reconcile_attempt(pk: int) -> None:
    """The per-identity worker: advance active work, hand off a retry that is still owed."""
    attempt = LifecycleAttempt.objects.get(pk=pk)
    if attempt.status == Status.REQUESTED:
        LifecycleAttempt.objects.filter(pk=pk, status=Status.REQUESTED).update(status=Status.RUNNING)
    elif attempt.status == Status.RETRYABLE_FAILED:
        with transaction.atomic():
            _create_successor(attempt)
        LifecycleAttempt.objects.filter(retry_of=attempt, status=Status.REQUESTED).update(status=Status.RUNNING)
    # Guarded heartbeat: matches nothing for a settled attempt, so it is not a write.
    LifecycleAttempt.objects.filter(pk=pk, status=Status.RUNNING).update(updated_at=timezone.now())


_WORKERS: dict[str, Callable[[int], None]] = {}


def drain_outbox() -> None:
    """Deliver everything published, including what delivery itself publishes."""
    while OUTBOX:
        task, pk = OUTBOX.pop(0)
        _WORKERS[task](pk)


def _recover_with(tick: Callable[[], int]) -> Callable[[], None]:
    def recover() -> None:
        # Three rounds, each past the recovery delay: enough to select, deliver and advance a successor.
        for round_number in range(1, 4):
            moment = timezone.now() + (RECOVERY_DELAY + timedelta(hours=1)) * round_number
            with current_host().require("frozen_clock")(moment):
                drain_outbox()
                tick()
                drain_outbox()

    return recover


recover = _recover_with(run_recovery_tick)
recover_nothing = _recover_with(run_inert_recovery_tick)

RETRY_DELIVERY = CallableDelivery(name="reference attempts", recover=recover, deliver=drain_outbox, lose=OUTBOX.clear)
INERT_DELIVERY = CallableDelivery(
    name="inert reference recovery", recover=recover_nothing, deliver=drain_outbox, lose=OUTBOX.clear
)


def fail_attempt_with_atomic_handoff(pk: int) -> None:
    """The conforming transition: one commit holds the failure and the successor it owes."""
    classify_retryable_failure_atomically(pk)


def fail_attempt_with_savepoint_handoff(pk: int) -> None:
    """One transaction with a nested savepoint: the savepoint is not a commit, so there is one boundary."""
    with transaction.atomic():
        LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)
        with transaction.atomic():
            _create_successor(LifecycleAttempt.objects.get(pk=pk))


def fail_attempt_with_split_handoff(pk: int) -> None:
    """The failure commits, then the successor commits separately."""
    classify_retryable_failure(pk)
    with transaction.atomic():
        _create_successor(LifecycleAttempt.objects.get(pk=pk))


def fail_attempt_in_autocommit(pk: int) -> None:
    """Two autocommit writes: each commits on its own, so the handoff is split without any atomic block."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)
    _create_successor(LifecycleAttempt.objects.get(pk=pk))


def fail_attempt_through_a_function_then_hand_off(pk: int) -> None:
    """The split handoff again, with the failure written by a SQL function in autocommit."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {FAIL_ATTEMPT_FUNCTION}(%s)", [pk])
    _create_successor(LifecycleAttempt.objects.get(pk=pk))


def fail_attempt_through_async_to_sync(pk: int) -> None:
    """The atomic handoff, reached from sync code through async_to_sync, as an async service layer would."""
    from asgiref.sync import async_to_sync, sync_to_async

    async def fail() -> None:
        await sync_to_async(fail_attempt_with_atomic_handoff)(pk)

    async_to_sync(fail)()


def fail_attempt_through_async_to_sync_collecting_on_the_way_out(pk: int) -> None:
    """The same, with a young collection while the error unwinds, as allocation in a close or a log would cause."""
    try:
        fail_attempt_through_async_to_sync(pk)
    finally:
        # Anything the in-flight error keeps alive survives this, and is promoted past generation 0.
        gc.collect(generation=0)


@transaction.atomic
def fail_attempt_with_commit_hook_handoff(pk: int) -> None:
    """The failure commits; the successor is created by an on_commit hook."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)
    transaction.on_commit(lambda: _create_successor(LifecycleAttempt.objects.get(pk=pk)))


def fail_attempt_with_finally_handoff(pk: int) -> None:
    """The successor is created in a finally block after the failure commits: a dead process never gets there."""
    try:
        classify_retryable_failure(pk)
    finally:
        with transaction.atomic():
            _create_successor(LifecycleAttempt.objects.get(pk=pk))


def fail_attempt_with_message_handoff(pk: int) -> None:
    """The failure commits and the retry exists only as a message published after commit."""
    classify_retryable_failure(pk)
    transaction.on_commit(lambda: publish("reconcile", pk))


AUDIT: list[int] = []


def record_audit(pk: int) -> None:
    """Unrelated after-commit bookkeeping, registered ahead of the handoff."""
    AUDIT.append(pk)


def fail_attempt_atomically_then_publishing(pk: int) -> None:
    """Conforming: the failure and its successor commit together; publishing after the commit only saves a tick."""
    classify_retryable_failure_atomically(pk)
    successor = LifecycleAttempt.objects.get(retry_of_id=pk)
    transaction.on_commit(lambda: publish("reconcile", successor.pk))


@transaction.atomic
def fail_attempt_publishing_after_an_audit(pk: int) -> None:
    """The retry exists only as a message published by the second of two plain after-commit callbacks."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)
    transaction.on_commit(lambda: record_audit(pk))
    transaction.on_commit(lambda: publish("reconcile", pk))


#: Attempts the reference notification task was run for.
NOTIFIED: list[int] = []


@functools.cache
def _notification_task() -> Any:
    """A real Celery task, run eagerly, standing for a webhook or email published after the commit."""
    from celery import Celery

    app = Celery("due-work-harness references", set_as_current=False)
    app.conf.task_always_eager = True

    @app.task(name="due-work-harness.references.notify")
    def notify(pk: int) -> None:
        NOTIFIED.append(pk)

    return notify


def notify_through_celery(pk: int) -> None:
    """Publish the attempt's notification through Celery."""
    _notification_task().delay(pk)


def fail_attempt_atomically_then_notifying(pk: int) -> None:
    """Conforming: the failure and its successor commit together; a notification is published after the commit."""
    classify_retryable_failure_atomically(pk)
    transaction.on_commit(lambda: notify_through_celery(pk))


@transaction.atomic
def fail_attempt_publishing_after_a_notification(pk: int) -> None:
    """The retry is handed off by the second callback, behind a first that publishes through Celery."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)
    transaction.on_commit(lambda: notify_through_celery(pk))
    transaction.on_commit(lambda: publish("reconcile", pk))


@transaction.atomic
def fail_attempt_publishing_after_a_robust_audit(pk: int) -> None:
    """The same, with robust callbacks: a failing audit no longer stops the publish, but a failing publish is lost."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)
    transaction.on_commit(lambda: record_audit(pk), robust=True)
    transaction.on_commit(lambda: publish("reconcile", pk), robust=True)


#: A signal the reference sends once a failure is classified, as a framework sends its hooks.
attempt_failed = Signal()


def audit_the_failure(sender: object, pk: int, **kwargs: object) -> None:
    """The signal's first receiver: unrelated bookkeeping."""
    record_audit(pk)


def reconcile_the_failure(sender: object, pk: int, **kwargs: object) -> None:
    """The signal's second receiver: it hands off the retry."""
    publish("reconcile", pk)


attempt_failed.connect(audit_the_failure, dispatch_uid="due-work-harness reference audit")
attempt_failed.connect(reconcile_the_failure, dispatch_uid="due-work-harness reference reconcile")


def fail_attempt_announcing_it(pk: int) -> None:
    """The retry is handed off by the second receiver of a signal sent after the commit."""
    classify_retryable_failure(pk)
    attempt_failed.send(sender=LifecycleAttempt, pk=pk)


def fail_attempt_atomically_then_announcing_it(pk: int) -> None:
    """Conforming: the failure and its successor commit together; the signal after it only saves a tick."""
    classify_retryable_failure_atomically(pk)
    attempt_failed.send(sender=LifecycleAttempt, pk=pk)


def fail_attempt_swallowing_the_death(pk: int) -> None:
    """A split handoff behind a catch-all that logs and carries on."""
    try:
        classify_retryable_failure(pk)
    except BaseException:  # noqa: BLE001 - the shape under test swallows even a process death
        pass
    RECIPIENT.notify(pk)


def fail_attempt_atomically_under_session_lock(pk: int) -> None:
    """The conforming atomic handoff, serialized by a session advisory lock."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [pk])
    try:
        classify_retryable_failure_atomically(pk)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [pk])


#: The external recipient is the framework-free reference's: sync, keyed, async and deferred seams.
RECIPIENT = Recipient()


def complete_attempt_notifying(pk: int) -> None:
    """Notify, then record completion: a death between the two leaves the attempt running."""
    RECIPIENT.notify(pk)
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)


def complete_attempt_notifying_async(pk: int) -> None:
    """Notify through an async client, then record completion."""
    asyncio.run(RECIPIENT.notify_async(pk))
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)


def complete_attempt_notifying_deferred(pk: int) -> None:
    """Notify through a sync method that returns the deferred effect, then record completion."""
    asyncio.run(RECIPIENT.deferred_notify(pk))
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)


def complete_attempt_notifying_once(pk: int) -> None:
    """The same, with an idempotency key the recipient honours."""
    RECIPIENT.notify_once(pk)
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)


@transaction.atomic
def complete_attempt_notifying_inside_its_transaction(pk: int) -> None:
    """Completion and the notification in one transaction: a death after the call rolls completion back."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.COMPLETE)
    RECIPIENT.notify(pk)


def _tick_publishing(task: str) -> Callable[[], int]:
    def tick() -> int:
        due = [attempt.pk for attempt in due_for_recovery()]
        for pk in due:
            publish(task, pk)
        return len(due)

    return tick


_WORKERS.update(
    reconcile=reconcile_attempt,
    notify=complete_attempt_notifying,
    notify_once=complete_attempt_notifying_once,
)

NOTIFYING_DELIVERY = CallableDelivery(
    name="reference notifications", recover=_recover_with(_tick_publishing("notify")), deliver=drain_outbox
)
NOTIFYING_ONCE_DELIVERY = CallableDelivery(
    name="reference keyed notifications", recover=_recover_with(_tick_publishing("notify_once")), deliver=drain_outbox
)


# --- Terminal-state (2b/2c) shapes ------------------------------------------------------------


class ReferenceLease:
    """Delivery ownership on the attempt row: claim and release write every row they touch."""

    duration = timedelta(minutes=5)

    def attempt_execution(self, pk: int, *, run: Callable[[int], None]) -> bool:
        now = timezone.now()
        claimed = (
            LifecycleAttempt.objects.filter(pk=pk)
            .exclude(reconciliation_claimed_at__gt=now - self.duration)
            .update(reconciliation_claimed_at=now)
        )
        if not claimed:
            return False
        try:
            run(pk)
        finally:
            LifecycleAttempt.objects.filter(pk=pk).update(reconciliation_claimed_at=None)
        return True


attempt_recovery = ReferenceLease()


def reconcile_attempt_under_lease(pk: int) -> None:
    """The reconcile worker behind the reference lease."""
    attempt_recovery.attempt_execution(pk, run=reconcile_attempt)


def lease_only(pk: int) -> None:
    """A worker whose application body does nothing: only the lease writes."""
    attempt_recovery.attempt_execution(pk, run=lambda _pk: None)


def reconcile_attempt_reopening(pk: int) -> None:
    """A worker that revives a completed attempt: a terminal state that was not final."""
    reconcile_attempt(pk)
    LifecycleAttempt.objects.filter(pk=pk, status=Status.COMPLETE).update(status=Status.REQUESTED)


def reconcile_attempt_after_commit(pk: int) -> None:
    """The retry handoff deferred to a commit callback."""
    attempt = LifecycleAttempt.objects.get(pk=pk)
    if attempt.status in (Status.REQUESTED, Status.RETRYABLE_FAILED):
        transaction.on_commit(lambda: _create_successor(attempt))


def reconcile_attempt_sending_handoff(pk: int) -> None:
    """A retry handed off only as a message published by name through ``Celery.send_task``."""
    attempt = LifecycleAttempt.objects.get(pk=pk)
    if attempt.status == Status.RETRYABLE_FAILED:
        from celery import current_app

        current_app.send_task("due_work_harness_references.reconcile", args=[pk])
        return
    reconcile_attempt(pk)


def reconcile_attempt_with_cte_handoff(pk: int) -> None:
    """The retry handoff written as a data-modifying CTE: no statement starts with INSERT."""
    attempt = LifecycleAttempt.objects.get(pk=pk)
    if attempt.status != Status.RETRYABLE_FAILED:
        reconcile_attempt(pk)
        return
    with connection.cursor() as cursor:
        cursor.execute(
            f"/* retry */ WITH predecessor AS (SELECT %s AS id) "  # noqa: S608 - fixed test table name
            f"INSERT INTO {LifecycleAttempt._meta.db_table} (status, retry_of_id, updated_at) "
            f"SELECT %s, id, %s FROM predecessor",
            [pk, Status.REQUESTED, timezone.now()],
        )


@transaction.atomic
def classify_retryable_failure_with_parked_successor(pk: int) -> None:
    """The failure commits with a successor parked outside the selection; only a message unparks it."""
    LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RETRYABLE_FAILED)
    LifecycleAttempt.objects.create(status=Status.TERMINAL_FAILED, retry_of_id=pk)


def reconcile_attempt_unparking(pk: int) -> None:
    """The predecessor's delivery flips its parked successor into the selection — an UPDATE, not an INSERT."""
    reconcile_attempt(pk)
    LifecycleAttempt.objects.filter(retry_of_id=pk, status=Status.TERMINAL_FAILED).update(status=Status.REQUESTED)


#: How long after a retryable failure the age-gated worker still hands off.
RETRY_WINDOW = timedelta(minutes=30)

#: How long the backoff-unparking worker parks its successor.
RETRY_BACKOFF = timedelta(hours=6)


def reconcile_attempt_within_retry_window(pk: int) -> None:
    """Hands off a successor only while the failure is fresh."""
    attempt = LifecycleAttempt.objects.get(pk=pk)
    if attempt.status == Status.RETRYABLE_FAILED:
        if attempt.updated_at >= timezone.now() - RETRY_WINDOW:
            with transaction.atomic():
                _create_successor(attempt)
        return
    reconcile_attempt(pk)


def reconcile_attempt_unparking_with_backoff(pk: int) -> None:
    """Unparks the parked successor with a backoff due time far past the recovery delay."""
    reconcile_attempt(pk)
    LifecycleAttempt.objects.filter(retry_of_id=pk, status=Status.TERMINAL_FAILED).update(
        status=Status.REQUESTED, updated_at=timezone.now() + RETRY_BACKOFF
    )


def reconcile_attempt_calling_provider(pk: int) -> None:
    """The reference worker, plus an external call for every attempt it is delivered — terminal ones too."""
    reconcile_attempt(pk)
    RECIPIENT.notify(pk)


def reconcile_attempt_calling_provider_for_active(pk: int) -> None:
    """An external call only for attempts still active: a terminal delivery is effect-free."""
    status = LifecycleAttempt.objects.get(pk=pk).status
    reconcile_attempt(pk)
    if status in ACTIVE_STATUSES:
        RECIPIENT.notify(pk)


def due_for_recovery_by_exclusion() -> QuerySet[LifecycleAttempt]:
    """The reference selection written as an exclusion of settled states."""
    return LifecycleAttempt.objects.exclude(
        status__in=(Status.COMPLETE, Status.TERMINAL_FAILED, Status.RETRYABLE_FAILED)
    ).filter(updated_at__lte=timezone.now() - RECOVERY_DELAY)
