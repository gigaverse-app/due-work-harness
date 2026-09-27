"""
The harness against procrastinate's own Django demo, unmodified.

Upstream: procrastinate @ 35f3ca98 (``procrastinate/demos/demo_django``).
Creating a book defers ``index_book``, which sleeps (standing in for slow
external work) and defers ``set_indexed``, which marks the book indexed. The
obligation: every created book ends up indexed.

Findings are strict xfails that carry their explanation; each has a positive
control showing the one change that makes the same proof pass. When upstream
changes the demo, the xfail turns red and this file must be updated.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.db import connection, connections
from django.test import Client
from procrastinate.contrib.django import app
from procrastinate.demos.demo_django.demo import tasks
from procrastinate.demos.demo_django.demo.models import Book

from due_work_harness import (
    CallableDelivery,
    HandoffHistory,
    MissingReclaim,
    assert_crash_at_every_commit_converges,
    assert_provider_call_holds_no_transaction,
)
from due_work_harness.integrations.procrastinate import (
    attempts_by_job,
    django_worker_once,
    redispatched_by,
    retry_stalled_jobs,
    strand_with_dead_worker,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def empty_queue() -> None:
    # pytest-django's flush leaves procrastinate's own tables alone; start each test empty.
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE procrastinate_events, procrastinate_jobs, procrastinate_workers CASCADE")


@pytest.fixture(autouse=True)
def instant_slow_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # EXTERNAL SEAM: the demo's time.sleep(5) stands in for slow external work.
    monkeypatch.setattr(tasks, "time", SimpleNamespace(sleep=lambda _seconds: None))


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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING: the view commits the book, then defers index_book in a second autocommit statement. "
        "A worker that dies between the two leaves the book never indexed, and nothing ever finds it again."
    ),
)
def test_creating_a_book_survives_a_worker_death_after_each_commit() -> None:
    assert_crash_at_every_commit_converges(WORKER, CREATE_BOOK)


def test_with_atomic_requests_creating_a_book_survives_every_death(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control, the one-setting fix: the book and its job commit together."""
    monkeypatch.setitem(connections.settings["default"], "ATOMIC_REQUESTS", True)
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    assert_crash_at_every_commit_converges(WORKER, CREATE_BOOK)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING: index_book is wrapped in @transaction.atomic around its slow call, so a real external call "
        "there would hold a transaction and a pooled connection for its whole latency."
    ),
)
def test_index_book_holds_no_transaction_during_its_slow_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def run_with_probe(probe) -> None:
        # EXTERNAL SEAM: the probe replaces the demo's slow call, at the same line.
        monkeypatch.setattr(tasks, "time", SimpleNamespace(sleep=lambda _seconds: probe()))
        book = Book.objects.create(title="Dune", author="Frank Herbert")
        tasks.index_book.defer(book_id=book.id)
        run_worker()

    assert_provider_call_holds_no_transaction(name="demo index_book", run_with_probe=run_with_probe)


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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING: the demo schedules no stalled-job recovery, so a job whose worker died stays 'doing' forever. "
        "Procrastinate documents the fix (a periodic retry_stalled_jobs task); the demo does not include it."
    ),
)
def test_a_job_whose_worker_died_is_picked_up_again() -> None:
    MissingReclaim(make_stranded=_stranded_job, dispatched_by_one_tick=lambda: redispatched_by(run_worker, _attempts))()


def test_with_the_documented_recipe_a_dead_workers_job_is_picked_up_again() -> None:
    """Positive control: procrastinate's own documented recipe reclaims the job."""

    def recipe_then_worker() -> None:
        asyncio.run(retry_stalled_jobs(app))
        run_worker()

    MissingReclaim(
        make_stranded=_stranded_job, dispatched_by_one_tick=lambda: redispatched_by(recipe_then_worker, _attempts)
    )()
