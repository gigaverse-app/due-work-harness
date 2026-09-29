"""
A :class:`~due_work_harness.host.SelectionInspector` for Django QuerySets on PostgreSQL.

Profile A's database proofs — the selection is index-served, it does not read a
replica, it does not discard most of what it reads, a tick issues a bounded
number of statements — ask the host rather than touching an ORM. This inspector
answers them for a ``due_work`` binding that returns a Django ``QuerySet``:

* **index served** — ``SET LOCAL enable_seqscan = off``, then ``EXPLAIN (FORMAT
  JSON)`` without executing the query, and the plan handed to
  :func:`due_work_harness.integrations.postgres_plans.index_served_verdict`. On
  the tiny tables a test database has, the planner would pick a sequential scan
  for anything, so cost comparison proves nothing. With sequential scans
  disabled, PostgreSQL falls back to one only when no usable index exists, which
  makes the plan structural evidence rather than a costing artifact;
* **replica read** — the database alias the QuerySet is routed to, compared
  against the aliases that are replicas;
* **scan counts** — ``EXPLAIN ANALYZE (FORMAT JSON)``, read by
  :func:`due_work_harness.integrations.postgres_plans.scan_counts`;
* **read costs** — ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` of the selection
  and of reading its whole table, after ``ANALYZE`` refreshed the statistics,
  read by :func:`due_work_harness.integrations.postgres_plans.read_cost`;
* **statements during a run** — Django's ``CaptureQueriesContext`` on one
  connection.
"""

import json
from collections.abc import Callable, Collection
from typing import Any
from unittest import mock

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Model, QuerySet
from django.test.utils import CaptureQueriesContext

from due_work_harness.host import ReadCosts
from due_work_harness.integrations.postgres_plans import index_served_verdict, read_cost, scan_counts
from due_work_harness.models import HarnessModel


def _require_postgresql(alias: str, reading: str) -> None:
    vendor = connections[alias].vendor
    assert vendor == "postgresql", (
        f"{reading} reads PostgreSQL EXPLAIN plans, and the selection's database {alias!r} is {vendor!r}. "
        f"Run this proof against PostgreSQL, or configure an inspector for this database"
    )


def _plan(explained: str | list[Any]) -> dict[str, Any]:
    # Depending on the driver, Django returns the JSON plan as text or decoded.
    document = json.loads(explained) if isinstance(explained, str) else explained
    return document[0]["Plan"]


def explain_index_eligibility(queryset: QuerySet[Any]) -> dict[str, Any]:
    """Probe index eligibility without executing the selected query or leaking settings."""
    _require_postgresql(queryset.db, "the index-served proof")
    database = connections[queryset.db]
    # A successful nested atomic block releases a savepoint, not SET LOCAL.
    # Restore explicitly on success; rollback owns restoration on SQL failure.
    # Use the queryset's actual connection for both EXPLAIN and its settings.
    with transaction.atomic(using=database.alias), database.cursor() as cursor:
        cursor.execute("SHOW enable_seqscan")
        previous = cursor.fetchone()[0]
        cursor.execute("SET LOCAL enable_seqscan = off")
        plan = _plan(queryset.explain(format="json"))
        cursor.execute("SELECT set_config('enable_seqscan', %s, true)", [previous])
    return plan


def _verified_partial_indexes(alias: str, table: str, named: Collection[str]) -> frozenset[str]:
    """The named indexes, once the catalog confirms each is a valid partial index on ``table``."""
    if not named:
        return frozenset()
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = %s::regclass AND i.indisvalid AND i.indpred IS NOT NULL",
            [connections[alias].ops.quote_name(table)],
        )
        partial = {name for (name,) in cursor.fetchall()}
    unverified = sorted(set(named) - partial)
    assert not unverified, (
        f"partial_indexes names {unverified}, which are not valid partial indexes on {table!r} "
        f"(partial ones are {sorted(partial)}). Naming a full index would excuse the whole-index read "
        f"the index-served proof exists to catch"
    )
    return frozenset(named)


def _mirrored_aliases() -> frozenset[str]:
    """Aliases Django's test settings declare as replicas (``TEST: {"MIRROR": ...}``)."""
    return frozenset(alias for alias, config in settings.DATABASES.items() if config.get("TEST", {}).get("MIRROR"))


class DjangoSelectionInspector(HarnessModel):
    """
    Database facts about a QuerySet selection.

    ``replica_aliases`` names the database aliases that are replicas: a
    collection, a callable evaluated at each check (for a project whose test
    settings move the replica alias per test), or ``None`` to use every alias
    whose ``TEST`` settings declare a ``MIRROR``. A project that declares no
    replica cannot select from one, so the replica check reports the primary.

    ``using`` is the connection whose statements :meth:`statements_during`
    counts.

    ``partial_indexes`` names partial indexes whose own WHERE clause narrows
    the selection: PostgreSQL may build a bitmap from one with no index
    condition, because the narrowing is in the index definition and EXPLAIN
    does not show it, so the verdict would reject the best plan there is. A
    name is only trusted after the catalog confirms it is a valid partial index
    on the selection's table; anything else fails the proof, since vouching for
    a full index would excuse the whole-index read the verdict exists to catch.
    """

    replica_aliases: Collection[str] | Callable[[], Collection[str]] | None = None
    using: str = DEFAULT_DB_ALIAS
    partial_indexes: Collection[str] = ()

    def understands(self, selection: object) -> bool:
        return isinstance(selection, QuerySet)

    def index_served(self, selection: object) -> tuple[bool, str]:
        queryset = self._queryset(selection)
        table = queryset.model._meta.db_table
        plan = explain_index_eligibility(queryset)
        verdict = index_served_verdict(
            plan, table=table, predicate_indexes=_verified_partial_indexes(queryset.db, table, self.partial_indexes)
        )
        if verdict is None:
            return True, json.dumps(plan, indent=2)
        return False, verdict

    def replica_read(self, selection: object) -> str | None:
        replicas = self._replicas()
        assert DEFAULT_DB_ALIAS not in replicas, (
            f"the test settings collapse the replica alias onto {DEFAULT_DB_ALIAS!r}, so this check cannot tell "
            f"the primary from the replica: it would report a violation for code that has none and miss one in "
            f"code that does. Restore a distinct replica alias for this test"
        )
        used = self._queryset(selection).db
        return used if used in replicas else None

    def scan_counts(self, selection: object) -> tuple[float, float]:
        queryset = self._queryset(selection)
        _require_postgresql(queryset.db, "the scan-ratio proof")
        return scan_counts(_plan(queryset.explain(analyze=True, format="json")))

    def read_costs(self, selection: object) -> ReadCosts:
        queryset = self._queryset(selection)
        _require_postgresql(queryset.db, "the retained-history proof")
        table = queryset.model._meta.db_table
        with connections[queryset.db].cursor() as cursor:
            # The planner chooses from statistics: without fresh ones a table of
            # thousands of rows just inserted looks empty, and any plan is "cheap".
            cursor.execute(f"ANALYZE {connections[queryset.db].ops.quote_name(table)}")
        whole_table = queryset.model._base_manager.using(queryset.db).all()
        return ReadCosts(
            selection=read_cost(_plan(queryset.explain(analyze=True, buffers=True, format="json")), table=table),
            full_table=read_cost(_plan(whole_table.explain(analyze=True, buffers=True, format="json")), table=table),
        )

    def statements_during(self, run: Callable[[], object]) -> list[str]:
        with CaptureQueriesContext(connections[self.using]) as captured:
            run()
        return [query["sql"] for query in captured.captured_queries]

    def _replicas(self) -> frozenset[str]:
        if self.replica_aliases is None:
            return _mirrored_aliases()
        if callable(self.replica_aliases):
            return frozenset(self.replica_aliases())
        return frozenset(self.replica_aliases)

    @staticmethod
    def _queryset(selection: object) -> QuerySet[Any]:
        assert isinstance(selection, QuerySet), (
            f"DjangoSelectionInspector reads QuerySets; the selection is {type(selection).__name__}"
        )
        return selection


def selection_built_by(tick: Callable[[], object], model: type[Model]) -> QuerySet[Any]:
    """
    The first ``model`` QuerySet a production tick evaluates, captured as it runs and returned unevaluated.

    For a sweep whose selection is built inline in its tick rather than in a
    callable of its own: binding ``due_work`` to this observes the production
    query instead of restating it, so the query cannot drift from the tick's.
    Run ``tick`` with its dispatches held (for Celery,
    :func:`due_work_harness.integrations.celery.held_publications`), so observing
    the selection hands off nothing. Each call runs the tick again, so the query
    reflects the current time and data, as production's next tick would.
    """
    captured: list[QuerySet[Any]] = []
    fetch_all = QuerySet._fetch_all

    def observed(queryset: QuerySet[Any]) -> None:
        if queryset.model is model and not captured:
            captured.append(queryset.all())
        fetch_all(queryset)

    with mock.patch.object(QuerySet, "_fetch_all", observed):
        tick()
    assert captured, f"the tick evaluated no {model.__name__} query, so there is no selection to observe"
    return captured[0]
