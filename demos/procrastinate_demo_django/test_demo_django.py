"""
procrastinate's own Django demo, unmodified, adopted as a project adopts the harness.

Upstream: procrastinate @ 35f3ca98 (``procrastinate/demos/demo_django``).
Creating a book defers ``index_book``, which sleeps (standing in for slow
external work) and defers ``set_indexed``, which marks the book indexed. The
obligation: every created book ends up indexed.

Two contracts, each a declaration and one decorated class, as an adopter
writes them. ``DEMO_AS_SHIPPED`` records what the harness finds as legacy
gaps, each a strict xfail carrying its explanation; it covers the create
view's handoff, so ``due-work-harness check`` counts it (see ``pyproject.toml``,
whose baseline holds the demo's other handoff). ``DEMO_WITH_ITS_FIXES`` is the
same obligation with the two documented fixes, and passes. When upstream
changes the demo, a strict xfail turns red and this file must change with it.
"""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.db import connection, connections
from django.test import Client
from procrastinate.contrib.django import app
from procrastinate.demos.demo_django.demo import tasks
from procrastinate.demos.demo_django.demo.models import Book
from procrastinate.demos.demo_django.demo.views import CreateBookView

from due_work_harness import (
    Adoption,
    CallableDelivery,
    Decline,
    DueWorkContract,
    DueWorkSource,
    ExtraProof,
    HandoffHistory,
    KnownGap,
    MissingReclaim,
    NotApplicable,
    Profile,
    SafetyContract,
    SafetyProfile,
    assert_provider_call_holds_no_transaction,
    due_work_contract_suite,
)
from due_work_harness.integrations.procrastinate import (
    attempts_by_job,
    django_worker_once,
    redispatched_by,
    retry_stalled_jobs,
    strand_with_dead_worker,
)


@pytest.fixture(autouse=True)
def empty_queue(django_db_blocker) -> Iterator[None]:  # noqa: ANN001
    # pytest-django's flush leaves procrastinate's own tables alone; start each case empty.
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("TRUNCATE procrastinate_events, procrastinate_jobs, procrastinate_workers CASCADE")


@pytest.fixture(autouse=True)
def instant_slow_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # EXTERNAL SEAM: the demo's time.sleep(5) stands in for slow external work.
    monkeypatch.setattr(tasks, "time", SimpleNamespace(sleep=lambda _seconds: None))


@pytest.fixture
def atomic_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one-setting fix: Django wraps each request in a transaction, so the book and its job commit together."""
    monkeypatch.setitem(connections.settings["default"], "ATOMIC_REQUESTS", True)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)


#: What the demo runs in production: `manage.py procrastinate worker` on its queue.
run_worker = django_worker_once(["index"])

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
    book = Book.objects.create(title="Dune", author="Frank Herbert")
    job_id = tasks.index_book.defer(book_id=book.id)
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


#: Nothing reclaims a job whose worker died: the worker alone never touches `doing` jobs.
UNRECLAIMED = MissingReclaim(
    make_stranded=_stranded_job, dispatched_by_one_tick=lambda: redispatched_by(run_worker, _attempts)
)
#: With procrastinate's documented recipe, the same job is picked up again.
RECLAIMED = MissingReclaim(
    make_stranded=_stranded_job, dispatched_by_one_tick=lambda: redispatched_by(_recipe_then_worker, _attempts)
)

WHY_NOT_A_SWEEP = (
    "the job row is the obligation and procrastinate's worker consumes it; there is no separate owed state "
    "for a sweep to select, and reclaiming a dead worker's job is profile B"
)
WHY_NO_AMBIGUITY = "index_book's slow call stands in for latency; it has no effect whose outcome can be unknown"
WHY_NO_RETENTION = "procrastinate keeps finished jobs; pruning them is its own remove_old_jobs task, outside the demo"
WHY_NO_CONVERGENCE = "each job sets one book's flag; no two results race to write the same row"
WHY_NOT_DERIVED = "the obligation is the job row committed with the book, not a fact derived from books"
SAFETY_AS_SHIPPED = {
    SafetyProfile.REPLAY_SAFE_EXECUTION: Decline("set_indexed only sets a flag, so a replay repeats nothing visible"),
    SafetyProfile.BOUNDED_RETRY: NotApplicable("the demo's tasks declare no retry policy"),
}

DEMO_AS_SHIPPED = DueWorkContract(
    name="procrastinate demo_django: create book",
    adoption=Adoption.LEGACY,
    transactional=True,
    profiles={
        Profile.A: Decline(WHY_NOT_A_SWEEP),
        Profile.B: KnownGap(
            "a job whose worker died stays 'doing' forever: the demo schedules no stalled-job recovery. "
            "Procrastinate documents the fix, a periodic retry_stalled_jobs task",
            detect=UNRECLAIMED,
        ),
        Profile.C: NotApplicable(WHY_NO_AMBIGUITY),
        Profile.D: Decline(WHY_NO_RETENTION),
        Profile.E: NotApplicable(WHY_NO_CONVERGENCE),
        Profile.F: Decline(WHY_NOT_DERIVED),
    },
    safety=SafetyContract(
        name="procrastinate demo_django: create book", adoption=Adoption.LEGACY, profiles=SAFETY_AS_SHIPPED
    ),
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
    ),
)


@due_work_contract_suite(DEMO_AS_SHIPPED, covers=(DueWorkSource(CreateBookView.form_valid),))
class TestTheDemoAsShipped:
    pass


DEMO_WITH_ITS_FIXES = DueWorkContract(
    name="procrastinate demo_django: create book, with its fixes",
    transactional=True,
    fixtures=("atomic_requests",),
    profiles={
        Profile.A: Decline(WHY_NOT_A_SWEEP),
        Profile.B: Decline(
            "reclaim is procrastinate's own documented retry_stalled_jobs task, proven by the extra proof "
            "'a dead worker's job is picked up again'; the lease itself is procrastinate's, not the demo's",
            prove=RECLAIMED,
        ),
        Profile.C: NotApplicable(WHY_NO_AMBIGUITY),
        Profile.D: Decline(WHY_NO_RETENTION),
        Profile.E: NotApplicable(WHY_NO_CONVERGENCE),
        Profile.F: Decline(WHY_NOT_DERIVED),
    },
    safety=SafetyContract(name="procrastinate demo_django: create book, with its fixes", profiles=SAFETY_AS_SHIPPED),
    handoffs=(CREATE_BOOK,),
    handoff_delivery=WORKER,
)


@due_work_contract_suite(DEMO_WITH_ITS_FIXES)
class TestTheDemoWithItsFixes:
    """With ATOMIC_REQUESTS the book and its job commit together; with retry_stalled_jobs a dead worker's job is reclaimed."""
