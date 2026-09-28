"""
procrastinate's own Django demo, unmodified, adopted as a project adopts the harness.

Upstream: procrastinate @ 35f3ca98 (``procrastinate/demos/demo_django``).
Creating a book defers ``index_book``, which sleeps (standing in for slow
external work) and defers ``set_indexed``, which marks the book indexed. The
obligation: every created book ends up indexed.

Where procrastinate has a capability, the contract claims it and the harness
proves it end to end against procrastinate's own machinery, through
``due_work_harness.integrations.procrastinate``: its workers, heartbeats and
documented reclaim (profile B), its ``remove_old_jobs`` pruning (profile D),
and, for the demo's task, its retry budget and replay. What the harness finds
is declared as legacy gaps, each a strict xfail; ``DEMO_WITH_ITS_FIXES`` is the
same obligation with the demo's two fixes, and passes. ``DEMO_AS_SHIPPED``
covers the create view's handoff, so ``due-work-harness check`` counts it (see
``pyproject.toml``, whose baseline holds the demo's other handoff).
"""

import asyncio
from collections import Counter
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import connection, connections
from django.test import Client
from procrastinate.contrib.django import app
from procrastinate.demos.demo_django.demo import tasks
from procrastinate.demos.demo_django.demo.models import Book
from procrastinate.demos.demo_django.demo.views import CreateBookView

from due_work_harness import (
    Adoption,
    BoundedRetry,
    CallableDelivery,
    Claim,
    Decline,
    DueWorkContract,
    DueWorkSource,
    ExtraProof,
    FencedOwnership,
    HandoffHistory,
    KnownGap,
    MissingReclaim,
    NotApplicable,
    Profile,
    ReplaySafeEffect,
    Retention,
    SafetyContract,
    SafetyProfile,
    assert_provider_call_holds_no_transaction,
    due_work_contract_suite,
)
from due_work_harness.contract import Disposition
from due_work_harness.integrations import procrastinate as integration
from due_work_harness.integrations.procrastinate import (
    attempts_by_job,
    django_worker_once,
    redispatched_by,
    retry_stalled_jobs,
    strand_with_dead_worker,
)

QUEUE = "index"

#: Calls to the demo's slow external work, and whether they fail: the seam the
#: demo already stubs (``time.sleep(5)``), counted for the replay and retry proofs.
SLOW_CALLS: Counter[str] = Counter()
FAILING = {"slow call": False}


@pytest.fixture(autouse=True)
def empty_queue(django_db_blocker) -> Iterator[None]:  # noqa: ANN001
    # pytest-django's flush leaves procrastinate's own tables alone; start each case empty.
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("TRUNCATE procrastinate_events, procrastinate_jobs, procrastinate_workers CASCADE")


@pytest.fixture(autouse=True)
def instant_slow_work(monkeypatch: pytest.MonkeyPatch) -> None:
    SLOW_CALLS.clear()
    FAILING["slow call"] = False

    def slow_call(_seconds: float) -> None:
        # EXTERNAL SEAM: the demo's time.sleep(5) stands in for slow external work.
        SLOW_CALLS["slow call"] += 1
        if FAILING["slow call"]:
            raise ConnectionError("the slow external dependency is down")

    monkeypatch.setattr(tasks, "time", SimpleNamespace(sleep=slow_call))


@pytest.fixture
def atomic_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one-setting fix: Django wraps each request in a transaction, so the book and its job commit together."""
    monkeypatch.setitem(connections.settings["default"], "ATOMIC_REQUESTS", True)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)


#: What the demo runs in production: `manage.py procrastinate worker` on its queue.
run_worker = django_worker_once([QUEUE])

# Procrastinate's queue is a table in the product database: no message can be
# lost apart from the database itself, so the delivery has no `lose`.
WORKER = CallableDelivery(name="procrastinate demo_django", recover=run_worker)


def create_book(title: str) -> None:
    """The demo's create view, through Django's full request stack."""
    response = Client().post("/create/", {"title": title, "author": "Frank Herbert"})
    assert response.status_code == 302, response.status_code


def indexed(title: str) -> list[bool]:
    return list(Book.objects.filter(title=title).values_list("indexed", flat=True))


CREATE_BOOK = HandoffHistory(
    name="create book",
    arrange=lambda: f"Dune {uuid4().hex[:8]}",
    transition=create_book,
    observe=indexed,
)


def defer_indexing() -> int:
    """The demo's own task, deferred for a new book: one job on the index queue."""
    book = Book.objects.create(title="Dune", author="Frank Herbert")
    return tasks.index_book.defer(book_id=book.id)


def the_demos_ownership() -> FencedOwnership:
    # ARRANGE: jobs deferred through the demo's own index_book task (defer_indexing).
    # REAL PRODUCTION: procrastinate's register_worker, fetch_job, finish_job, heartbeat and reclaim recipe.
    # EXTERNAL SEAM: none; the integration expires a worker's heartbeat to stand for its death.
    # OBSERVE: the job's status and owner, and the owner's heartbeat.
    return integration.ownership(app, defer=defer_indexing, queue=QUEUE)


def the_demos_retention() -> Retention:
    # ARRANGE: an owed job and a finished one, both older than the retention window.
    # REAL PRODUCTION: procrastinate's delete_old_jobs, which its builtin remove_old_jobs task runs.
    # EXTERNAL SEAM: none.
    # OBSERVE: whether each job row still exists.
    return integration.retention(app, defer=defer_indexing, max_hours=24)


def index_again(book_id: int) -> None:
    """The demo's index_book run for a book, then the worker that runs what it deferred."""
    tasks.index_book(book_id=book_id)
    run_worker()


def indexing_replay() -> ReplaySafeEffect:
    # ARRANGE: a book to index (prepare).
    # REAL PRODUCTION: the demo's index_book, then procrastinate's worker running set_indexed (index_again).
    # EXTERNAL SEAM: the slow external call, counted per replay (SLOW_CALLS).
    # OBSERVE: whether the book is indexed.
    return ReplaySafeEffect(
        name="index_book",
        prepare=lambda: Book.objects.create(title="Dune", author="Frank Herbert").id,
        execute=index_again,
        observe=lambda book_id: Book.objects.get(id=book_id).indexed,
        execution_count_for=lambda _book_id: SLOW_CALLS["slow call"],
    )


def _job(job_id: int) -> tuple[str, int]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT status, attempts FROM procrastinate_jobs WHERE id = %s", [job_id])
        return cursor.fetchone()


def _todo_jobs() -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM procrastinate_jobs WHERE status = 'todo'")
        return [job_id for (job_id,) in cursor.fetchall()]


def _failing_indexing() -> int:
    FAILING["slow call"] = True
    return defer_indexing()


def _run_the_worker(_job_id: int) -> None:
    call_command("procrastinate", "worker", "--queues", QUEUE, "--one-shot", "--no-listen-notify")


def indexing_retry() -> BoundedRetry:
    # ARRANGE: a job whose slow external dependency is down (_failing_indexing).
    # REAL PRODUCTION: `manage.py procrastinate worker`, as the demo runs it (_run_the_worker).
    # EXTERNAL SEAM: the slow call, which fails and is counted (SLOW_CALLS).
    # OBSERVE: the job's status and attempts, as procrastinate records them.
    return BoundedRetry(
        name="index_book",
        # The demo's tasks declare no retry: one execution, then failed for good.
        max_executions=1,
        make_failing=_failing_indexing,
        due_work=_todo_jobs,
        run_once=_run_the_worker,
        advance_to_due=lambda _job_id: None,
        is_terminal=lambda job_id: _job(job_id)[0] == "failed",
        failure_attempt_count=lambda _job_id: SLOW_CALLS["slow call"],
        observe=_job,
    )


def _index_a_book_with(probe) -> None:  # noqa: ANN001
    # EXTERNAL SEAM: the probe replaces the demo's slow call, at the same line.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tasks, "time", SimpleNamespace(sleep=lambda _seconds: probe()))
        # ARRANGE: a book and its job, as the create view leaves them.
        book = Book.objects.create(title="Dune", author="Frank Herbert")
        tasks.index_book.defer(book_id=book.id)
        # REAL PRODUCTION: procrastinate's worker runs index_book.
        run_worker()


def index_book_holds_no_transaction() -> None:
    # ARRANGE: a book and its job (_index_a_book_with).
    # REAL PRODUCTION: procrastinate's worker running the demo's index_book (_index_a_book_with).
    # EXTERNAL SEAM: the probe stands in for the demo's slow call, at the same line.
    # OBSERVE: the shared proof watches the transaction state at the slow call.
    assert_provider_call_holds_no_transaction(name="demo index_book", run_with_probe=_index_a_book_with)


def _stranded_job() -> int:
    """FAULT INJECTION: a job a worker fetched and then died holding."""
    job_id = defer_indexing()
    with connection.cursor() as cursor:
        strand_with_dead_worker(cursor, job_id)
    return job_id


def _attempts() -> dict[int, int]:
    with connection.cursor() as cursor:
        return attempts_by_job(cursor)


def _recipe_then_worker() -> None:
    """Procrastinate's documented recipe: a periodic retry_stalled_jobs, then the worker."""
    asyncio.run(retry_stalled_jobs(app))
    run_worker()


#: What the demo runs: the worker alone, which never touches a dead worker's `doing` job.
THE_DEMOS_WORKER_RECLAIMS = MissingReclaim(
    make_stranded=_stranded_job, dispatched_by_one_tick=lambda: redispatched_by(run_worker, _attempts)
)
#: With procrastinate's documented recipe scheduled, the same job is picked up again.
THE_RECIPE_RECLAIMS = MissingReclaim(
    make_stranded=_stranded_job, dispatched_by_one_tick=lambda: redispatched_by(_recipe_then_worker, _attempts)
)

WHY_NOT_A_SWEEP = (
    "procrastinate's worker polls the job table, so a lost notification never strands a job; its selection "
    "claims as it selects, inside procrastinate_fetch_job, so there is no separate owed state for a sweep to "
    "select. Reclaiming a dead worker's job is profile B, and scheduling that reclaim is an extra proof here"
)
WHY_NO_CONVERGENCE = "each job sets one book's flag; no two results race to write the same row"
WHY_NOT_DERIVED = (
    "the obligation is the job row committed with the book, not a fact derived from books: a book created "
    "without its job is never indexed, which is procrastinate's model rather than a defect"
)
STALE_WORKER_FINISHES = (
    "procrastinate's finish_job and retry_job update a job by id without checking its worker: a worker presumed "
    "dead, whose job was reclaimed and fetched by another worker, can still mark it finished, or send it back to "
    "todo to run a second time, while the new owner runs it"
)


def _profiles() -> dict[Profile, Disposition]:
    return {
        Profile.A: Decline(WHY_NOT_A_SWEEP),
        Profile.B: Claim(gaps={"assert_stale_token_is_rejected": STALE_WORKER_FINISHES}),
        Profile.C: KnownGap(
            "index_book records no attempt before its slow external call, so after a worker dies nothing can "
            "tell whether the call happened: reclaim reruns it blind"
        ),
        Profile.D: Claim(),
        Profile.E: NotApplicable(WHY_NO_CONVERGENCE),
        Profile.F: Decline(WHY_NOT_DERIVED),
    }


def _safety(name: str) -> SafetyContract:
    return SafetyContract(
        name=name,
        profiles={SafetyProfile.REPLAY_SAFE_EXECUTION: Claim(), SafetyProfile.BOUNDED_RETRY: Claim()},
        replay=indexing_replay,
        retry=indexing_retry,
        transactional=True,
    )


DEMO_AS_SHIPPED = DueWorkContract(
    name="procrastinate demo_django: create book",
    adoption=Adoption.LEGACY,
    transactional=True,
    profiles=_profiles(),
    ownership=the_demos_ownership,
    retention=the_demos_retention,
    safety=_safety("procrastinate demo_django: create book"),
    handoffs=(CREATE_BOOK,),
    handoff_delivery=WORKER,
    handoff_gaps={
        "create book": (
            "the view commits the book, then defers index_book in a second autocommit statement. A worker "
            "that dies between the two leaves the book never indexed, and nothing ever finds it again"
        )
    },
    extras=(
        ExtraProof(
            name="index_book holds no transaction during its slow work",
            run=index_book_holds_no_transaction,
            transactional=True,
            gap=(
                "index_book is wrapped in @transaction.atomic around its slow call, so a real external call "
                "there would hold a transaction and a pooled connection for its whole latency"
            ),
        ),
        ExtraProof(
            name="a dead worker's job is picked up again by what the demo runs",
            run=THE_DEMOS_WORKER_RECLAIMS,
            transactional=True,
            gap=(
                "the demo schedules no stalled-job recovery, so a job whose worker died stays 'doing' forever. "
                "Procrastinate documents the fix, a periodic retry_stalled_jobs task"
            ),
        ),
    ),
)


@due_work_contract_suite(DEMO_AS_SHIPPED, covers=(DueWorkSource(CreateBookView.form_valid),))
class TestTheDemoAsShipped:
    pass


DEMO_WITH_ITS_FIXES = DueWorkContract(
    name="procrastinate demo_django: create book, with its fixes",
    # Legacy only for profile B's gap, which is procrastinate's own, not the demo's.
    adoption=Adoption.LEGACY,
    transactional=True,
    fixtures=("atomic_requests",),
    profiles=_profiles(),
    ownership=the_demos_ownership,
    retention=the_demos_retention,
    safety=_safety("procrastinate demo_django: create book, with its fixes"),
    handoffs=(CREATE_BOOK,),
    handoff_delivery=WORKER,
    extras=(
        ExtraProof(
            name="a dead worker's job is picked up again by procrastinate's recipe",
            run=THE_RECIPE_RECLAIMS,
            transactional=True,
        ),
    ),
)


@due_work_contract_suite(DEMO_WITH_ITS_FIXES)
class TestTheDemoWithItsFixes:
    """With ATOMIC_REQUESTS the book and its job commit together; with retry_stalled_jobs a dead worker's job is reclaimed."""
