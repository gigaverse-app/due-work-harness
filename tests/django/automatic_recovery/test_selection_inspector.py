"""
Profile A's database proofs through the Django host, against real PostgreSQL.

The core self-tests drive these proofs with an in-memory inspector; here the
Django inspector answers them from the database itself, in both directions:

* **index served** (invariant 5) — the reference selection with and without a
  partial index that serves it, and the ``SET LOCAL`` probe leaving the
  caller's session and transaction as it found them;
* **primary reads** (invariant 10) — a QuerySet routed to a replica alias, and
  a test setup that collapses the replica onto the primary, which must refuse
  rather than answer;
* **statement counts** — an idle tick and a tick whose cost grows per row;
* **scan ratio** — a selection that reads a table of settled rows to return a
  few owed ones, with and without the index;
* **schedule evidence** read from Django's ``CELERY_BEAT_SCHEDULE``.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from pytest_obligation.contract import ScheduledSelection
from pytest_obligation.helpers import undeclared
from pytest_obligation.host import Host, hosted
from pytest_obligation.integrations.celery import celery_beat_evidence, celery_beat_interval, celery_publications
from pytest_obligation.integrations.django import django_host
from pytest_obligation.integrations.django import lifecycle_references as ref
from pytest_obligation.integrations.django.selection import DjangoSelectionInspector, explain_index_eligibility
from pytest_obligation.integrations.postgres_plans import iter_plan_nodes
from pytest_obligation.profiles.automatic_recovery import (
    DueWorkSweep,
    assert_idle_tick_is_cheap,
    assert_selection_cost_does_not_grow_with_the_history,
    assert_selection_does_not_read_the_replica,
    assert_selection_is_index_served,
    assert_selection_scan_ratio_is_bounded,
    assert_tick_cost_does_not_grow_with_the_backlog,
)

pytestmark = pytest.mark.django_db(transaction=True)
Status = ref.Status

_TABLE = ref.LifecycleAttempt._meta.db_table
_DUE_INDEX = "lifecycle_attempt_due_ix"


@pytest.fixture(autouse=True)
def attempt_table() -> Iterator[None]:
    with ref.lifecycle_attempt_table():
        yield


def _add_due_index() -> None:
    """A partial index whose predicate is the selection's state filter, ordered as the selection reads."""
    active = ", ".join(f"'{status}'" for status in ref.ACTIVE_STATUSES)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX {_DUE_INDEX} ON {_TABLE} (updated_at, id) WHERE status IN ({active})")


def _make(status: str, *, age: timedelta) -> ref.LifecycleAttempt:
    # ARRANGE: persist one attempt and backdate it to simulate elapsed time.
    return ref.LifecycleAttempt.objects.create(status=status, updated_at=timezone.now() - age)


def _make_owed(*, age: timedelta = timedelta(hours=1)) -> ref.LifecycleAttempt:
    return _make(Status.REQUESTED, age=age)


def _scheduled(due_work: Any) -> ScheduledSelection:
    return ScheduledSelection(
        name="reference attempt selection", due_work=due_work, unscheduled_because="harness self-test of the plan"
    )


def _sweep(due_work: Any = ref.due_for_recovery) -> DueWorkSweep:
    refuse = undeclared("only the database proofs are applied in this test")
    return DueWorkSweep(
        name="reference attempt lifecycle",
        due_work=due_work,
        run_tick=ref.run_recovery_tick,
        make_owed=_make_owed,
        make_terminal=refuse,
        recovery_delay=ref.RECOVERY_DELAY,
        page_size=None,
    )


# --- Invariant 5: index served ------------------------------------------------------


def test_the_reference_selection_is_index_served_once_its_index_exists() -> None:
    with pytest.raises(AssertionError, match=r"sequential\s+scan|full scan by another name"):
        assert_selection_is_index_served(_scheduled(ref.due_for_recovery))
    _add_due_index()
    assert_selection_is_index_served(_scheduled(ref.due_for_recovery))


def test_an_index_on_another_column_does_not_serve_the_selection() -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX lifecycle_attempt_claimed_ix ON {_TABLE} (reconciliation_claimed_at)")
    with pytest.raises(AssertionError, match=r"sequential\s+scan|full scan by another name"):
        assert_selection_is_index_served(_scheduled(ref.due_for_recovery))


def test_index_proof_owns_its_set_local_transaction_in_autocommit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert connection.get_autocommit()
    original = QuerySet.explain
    observed = []

    def explain(queryset: QuerySet[Any], *args: Any, **kwargs: Any) -> str:
        assert connection.in_atomic_block
        with connection.cursor() as cursor:
            cursor.execute("SHOW enable_seqscan")
            observed.append(cursor.fetchone()[0])
        return original(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "explain", explain)

    def select_attempts() -> QuerySet[ref.LifecycleAttempt]:
        # ARRANGE: no rows are needed to inspect the primary-key index.
        # REAL PRODUCTION: a real ORM queryset is EXPLAINed against PostgreSQL.
        # EXTERNAL SEAM: none; the spy above observes the live session setting.
        # OBSERVE: the proof reads the real plan; the spy reads SET LOCAL.
        return ref.LifecycleAttempt.objects.filter(pk=0)

    assert_selection_is_index_served(_scheduled(select_attempts))
    assert observed == ["off"]
    assert connection.get_autocommit()


@pytest.mark.parametrize("previous", ["on", "off"])
@pytest.mark.parametrize("invalid_query", [False, True], ids=["success", "database-error"])
def test_index_proof_preserves_enclosing_transaction_settings(previous: str, invalid_query: bool) -> None:
    def select_attempts() -> QuerySet[ref.LifecycleAttempt]:
        # ARRANGE: an empty primary-key selection or deliberately invalid SQL.
        # REAL PRODUCTION: Django's actual PostgreSQL EXPLAIN path.
        # EXTERNAL SEAM: none.
        # OBSERVE: session settings and the enclosing transaction remain usable.
        query = ref.LifecycleAttempt.objects.filter(pk=0)
        return query.extra(where=["missing_eligibility_column = 1"]) if invalid_query else query

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('enable_seqscan', %s, true)", [previous])
        if invalid_query:
            with pytest.raises(DatabaseError, match="missing_eligibility_column"):
                assert_selection_is_index_served(_scheduled(select_attempts))
        else:
            assert_selection_is_index_served(_scheduled(select_attempts))
        cursor.execute("SHOW enable_seqscan")
        assert cursor.fetchone()[0] == previous, "eligibility probe changed the caller's planner setting"
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,), "failed EXPLAIN must not poison the enclosing transaction"
        transaction.set_rollback(True)


def test_a_selection_the_django_inspector_cannot_read_names_the_capability() -> None:
    with pytest.raises(AssertionError, match="needs a SelectionInspector that understands list"):
        assert_selection_is_index_served(_sweep(lambda: list(ref.due_for_recovery())))


# --- Invariant 10: primary reads ------------------------------------------------------


def _replica_host(replicas: Any) -> Any:
    return hosted(django_host(set(), replica_aliases=replicas, publication_recorder=celery_publications))


@pytest.mark.parametrize("replicas", [{"replica"}, lambda: {"replica"}], ids=["collection", "callable"])
def test_a_selection_routed_to_the_replica_is_caught(replicas: Any) -> None:
    with _replica_host(replicas):
        assert_selection_does_not_read_the_replica(_scheduled(ref.due_for_recovery))
        with pytest.raises(AssertionError, match="reads from 'replica'"):
            assert_selection_does_not_read_the_replica(_sweep(lambda: ref.due_for_recovery().using("replica")))


def test_a_replica_collapsed_onto_the_primary_is_refused_rather_than_answered() -> None:
    with _replica_host({"default"}), pytest.raises(AssertionError, match="collapse the replica alias onto 'default'"):
        assert_selection_does_not_read_the_replica(_scheduled(ref.due_for_recovery))


def test_a_project_that_declares_no_replica_reads_the_primary() -> None:
    # The test settings mirror no alias, so no alias is a replica.
    assert_selection_does_not_read_the_replica(_sweep(lambda: ref.due_for_recovery().using("replica")))


# --- Statement counts -----------------------------------------------------------------


def test_the_reference_tick_costs_one_statement_idle_and_busy() -> None:
    assert_idle_tick_is_cheap(name="idle reference tick", run_tick=ref.run_recovery_tick, max_queries=1)
    assert_tick_cost_does_not_grow_with_the_backlog(
        name="reference tick", make_owed=_make_owed, run_tick=ref.run_recovery_tick
    )


def test_a_tick_that_loads_each_row_is_caught() -> None:
    def tick_loading_each_row() -> int:
        due = list(ref.due_for_recovery().values_list("pk", flat=True))
        for pk in due:
            ref.LifecycleAttempt.objects.get(pk=pk)
        return len(due)

    with pytest.raises(AssertionError, match="cost grows per row"):
        assert_tick_cost_does_not_grow_with_the_backlog(
            name="per-row loads", make_owed=_make_owed, run_tick=tick_loading_each_row
        )


def test_an_idle_tick_that_queries_more_than_it_selects_is_caught() -> None:
    def chatty_idle_tick() -> int:
        ref.LifecycleAttempt.objects.count()
        ref.LifecycleAttempt.objects.filter(status=Status.RUNNING).exists()
        return ref.run_recovery_tick()

    with pytest.raises(AssertionError, match="an idle tick issued 3 queries"):
        assert_idle_tick_is_cheap(name="chatty idle tick", run_tick=chatty_idle_tick)


# --- Scan ratio -------------------------------------------------------------------------


def _settled_history(rows: int) -> None:
    """A table mostly of settled attempts, as production's is, then fresh planner statistics."""
    old = timezone.now() - timedelta(days=30)
    ref.LifecycleAttempt.objects.bulk_create(
        ref.LifecycleAttempt(status=Status.COMPLETE, updated_at=old) for _ in range(rows)
    )
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {_TABLE}")


def test_a_selection_that_reads_the_settled_history_fails_the_scan_ratio() -> None:
    _settled_history(2000)
    with pytest.raises(AssertionError, match="discarded 2000 rows to return 50"):
        assert_selection_scan_ratio_is_bounded(_sweep(), rows=50)


def test_an_indexed_selection_passes_the_scan_ratio() -> None:
    _settled_history(2000)
    _add_due_index()
    assert_selection_scan_ratio_is_bounded(_sweep(), rows=50)


# --- Schedule evidence from Django settings -------------------------------------------

_SCHEDULED_TASK = "pytest_obligation.integrations.django.lifecycle_references.run_recovery_tick"


def test_beat_evidence_reads_the_django_schedule(settings: Any) -> None:
    settings.CELERY_BEAT_SCHEDULE = {"recovery": {"task": _SCHEDULED_TASK, "schedule": timedelta(minutes=5)}}
    celery_beat_evidence(_SCHEDULED_TASK)()
    assert celery_beat_interval(_SCHEDULED_TASK) == timedelta(minutes=5)


def test_beat_evidence_rejects_a_task_the_django_schedule_omits(settings: Any) -> None:
    settings.CELERY_BEAT_SCHEDULE = {}
    with pytest.raises(AssertionError, match="not in the beat schedule"):
        celery_beat_evidence(_SCHEDULED_TASK)()


# --- The selection reads the owed rows, not the history behind them ------------------

_HISTORY_ROWS = 6000


def _history_sweep() -> DueWorkSweep:
    return _sweep().model_copy(update={"make_terminal": lambda *, age: _make(Status.COMPLETE, age=age)})


def _history_proof() -> None:
    assert_selection_cost_does_not_grow_with_the_history(_history_sweep(), history_rows=_HISTORY_ROWS)


def test_a_selection_served_by_its_partial_index_reads_only_the_owed_rows() -> None:
    _add_due_index()
    _history_proof()


def test_a_selection_with_no_index_reads_the_whole_history() -> None:
    with pytest.raises(AssertionError, match="scans its table sequentially"):
        _history_proof()


def test_an_index_that_reads_its_whole_range_and_filters_it_still_reads_the_history() -> None:
    # Every settled row is old, so the selection's time bound narrows nothing: an index on it is walked end
    # to end and the settled rows are discarded one by one. The planner would rather scan the table, so
    # the sequential scan is taken away, as an index-served proof would.
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX lifecycle_attempt_updated_ix ON {_TABLE} (updated_at, id)")
        cursor.execute("SET enable_seqscan = off")
    try:
        with pytest.raises(AssertionError, match=r"visited \d+ rows to find 5 owed ones"):
            _history_proof()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET enable_seqscan")


# --- Partial indexes the adopter vouches for -----------------------------------------


@pytest.fixture
def bitmap_over_a_partial_index() -> Iterator[str]:
    """
    A partial index that narrows a selection through its WHERE clause alone, read as a bitmap.

    The index is keyed on ``id``, which no selection here constrains, so the plan's bitmap
    index scan has no index condition: exactly what the verdict rejects for a full index.
    """
    active = ", ".join(f"'{status}'" for status in ref.ACTIVE_STATUSES)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX lifecycle_attempt_active_ix ON {_TABLE} (id) WHERE status IN ({active})")
        cursor.execute("SET enable_indexscan = off")
    try:
        yield "lifecycle_attempt_active_ix"
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET enable_indexscan")


def _index_served_with(*partial_indexes: str, due_work: Any = ref.due_for_recovery) -> None:
    inspector = DjangoSelectionInspector(partial_indexes=partial_indexes)
    with hosted(Host(selection_inspectors=(inspector,))):
        assert_selection_is_index_served(_scheduled(due_work))


def _active_attempts() -> QuerySet[ref.LifecycleAttempt]:
    # ARRANGE: none; the partial index fixture builds the index this selection's predicate matches.
    # REAL PRODUCTION: a real ORM queryset whose whole predicate is the partial index's WHERE.
    # EXTERNAL SEAM: none.
    # OBSERVE: the proof reads the real PostgreSQL plan.
    return ref.LifecycleAttempt.objects.filter(status__in=ref.ACTIVE_STATUSES)


def test_a_bitmap_over_a_partial_index_is_rejected_until_the_adopter_names_the_index(
    bitmap_over_a_partial_index: str,
) -> None:
    with pytest.raises(AssertionError, match="builds a bitmap.*no index condition"):
        _index_served_with(due_work=_active_attempts)
    _index_served_with(bitmap_over_a_partial_index, due_work=_active_attempts)


def test_a_named_partial_index_serves_a_time_bounded_selection(bitmap_over_a_partial_index: str) -> None:
    # The reference selection also bounds updated_at, which the index's WHERE cannot hold: the heap scan
    # filters owed rows that are not due yet. That is backlog, not history, and the index serves it.
    _index_served_with(bitmap_over_a_partial_index)


@pytest.mark.parametrize("analyzed", [False, True], ids=["default-statistics", "fresh-statistics"])
def test_a_named_partial_index_serves_the_selection_whichever_scan_the_planner_picks(analyzed: bool) -> None:
    # Without forcing a bitmap, the planner may walk the partial index and filter the time bound: a plain
    # Index Scan with no Index Cond. The verdict accepts it for a named partial index, as it accepts the bitmap.
    active = ", ".join(f"'{status}'" for status in ref.ACTIVE_STATUSES)
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX lifecycle_attempt_active_ix ON {_TABLE} (id) WHERE status IN ({active})")
        if analyzed:
            cursor.execute(f"ANALYZE {_TABLE}")
    with pytest.raises(AssertionError, match=r"no index condition|full scan by another name"):
        _index_served_with()
    _index_served_with("lifecycle_attempt_active_ix")


def test_naming_an_index_that_is_not_partial_is_refused(bitmap_over_a_partial_index: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX lifecycle_attempt_full_ix ON {_TABLE} (id)")
    with pytest.raises(AssertionError, match=r"partial_indexes names \['lifecycle_attempt_full_ix'\]"):
        _index_served_with(bitmap_over_a_partial_index, "lifecycle_attempt_full_ix")


def test_naming_an_index_that_does_not_exist_is_refused() -> None:
    with pytest.raises(AssertionError, match=r"partial_indexes names \['no_such_ix'\]"):
        _index_served_with("no_such_ix")


@pytest.mark.parametrize(
    "index",
    ["(id) WHERE status IS NOT NULL", "(updated_at) WHERE status IS NOT NULL", "(updated_at) WHERE status <> 'zzz'"],
)
def test_a_named_partial_index_whose_predicate_the_filter_re_tests_is_refused(index: str) -> None:
    # Each predicate constrains status and holds for the settled history too, so the plan filters status again.
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX lifecycle_attempt_weak_ix ON {_TABLE} {index}")
        cursor.execute("SET enable_indexscan = off" if index.startswith("(id)") else "SELECT 1")
    try:
        with pytest.raises(AssertionError, match=r"lifecycle_attempt_weak_ix.*constrains \['status'\]"):
            _index_served_with("lifecycle_attempt_weak_ix")
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET enable_indexscan")


def test_a_time_index_whose_predicate_says_nothing_fails_the_history_proof() -> None:
    # ``(updated_at) WHERE updated_at IS NOT NULL`` serves the plan as any index on the time bound does: an Index
    # Cond narrows by time, which the index-served verdict accepts, named or not. Every settled row is old enough
    # to pass that bound, so only the retained-history proof, which measures what the selection reads, refuses it.
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE INDEX lifecycle_attempt_time_ix ON {_TABLE} (updated_at) WHERE updated_at IS NOT NULL")
    _index_served_with("lifecycle_attempt_time_ix")
    with pytest.raises(AssertionError, match=r"scans its table sequentially|visited \d+ rows|buffers against"):
        _history_proof()


def test_a_named_partial_index_that_narrows_the_history_passes_the_history_proof(
    bitmap_over_a_partial_index: str,
) -> None:
    # The conforming claim: the index's WHERE leaves the settled history out, so its bitmap reads little.
    _index_served_with(bitmap_over_a_partial_index)
    _history_proof()


# --- A tiny table walking an unrelated index -------------------------------------------

_WALKED = {
    "Node Type": "Index Scan",
    "Relation Name": _TABLE,
    "Index Name": f"{_TABLE}_pkey",
    "Filter": "(((status)::text = ANY ('{requested,running}'::text[])) AND (updated_at <= $1))",
}


@pytest.mark.parametrize(
    ("index_cond", "served"), [("(updated_at <= $1)", True), ("((status)::text = 'requested')", False)]
)
def test_the_inspector_asks_the_bitmap_plan_when_the_first_walks_an_unrelated_index(
    monkeypatch: pytest.MonkeyPatch, index_cond: str, served: bool
) -> None:
    # The planner's first pick is scripted (it depends on statistics a test cannot pin); the inspector's
    # second question and its verdict are what is under test.
    from pytest_obligation.integrations.django import selection

    bitmap = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "due_ix", "Index Cond": index_cond}],
    }
    asked: list[bool] = []

    def scripted(queryset: QuerySet[Any], *, bitmap_only: bool = False) -> dict[str, Any]:
        asked.append(bitmap_only)
        return bitmap if bitmap_only else _WALKED

    monkeypatch.setattr(selection, "explain_index_eligibility", scripted)
    verdict, _evidence = DjangoSelectionInspector().index_served(ref.due_for_recovery())
    assert (verdict, asked) == (served, [False, True])


_PLANNER_SETTINGS = ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan")


@pytest.mark.parametrize("starts_off", _PLANNER_SETTINGS)
def test_the_bitmap_only_probe_disables_plain_index_scans_and_restores_every_setting(starts_off: str) -> None:
    _add_due_index()
    observed: dict[str, str] = {}
    original = QuerySet.explain

    def explain(queryset: QuerySet[Any], *args: Any, **kwargs: Any) -> str:
        with connection.cursor() as cursor:
            for setting in ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan"):
                cursor.execute(f"SHOW {setting}")
                observed[setting] = cursor.fetchone()[0]
        return original(queryset, *args, **kwargs)

    settings = _PLANNER_SETTINGS
    # Inside the caller's transaction, where a SET LOCAL left behind would outlive the probe; one setting
    # starts off, so restoring means restoring each one's own value, and the other two catch a missed restore.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL {starts_off} = off")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(QuerySet, "explain", explain)
            plan = explain_index_eligibility(ref.due_for_recovery(), bitmap_only=True)
        after = {}
        for setting in settings:
            cursor.execute(f"SHOW {setting}")
            after[setting] = cursor.fetchone()[0]
    assert observed == dict.fromkeys(settings, "off")
    assert any(node.get("Node Type") == "Bitmap Index Scan" for node in iter_plan_nodes(plan))
    assert after == {setting: "off" if setting == starts_off else "on" for setting in settings}
