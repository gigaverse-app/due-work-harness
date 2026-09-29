"""
What a PostgreSQL ``EXPLAIN (FORMAT JSON)`` plan says about a scheduled selection.

Pure functions over the plan tree PostgreSQL returns: no database driver, no ORM,
no connection. Any PostgreSQL integration captures a plan its own way and hands
the ``"Plan"`` object here for the verdict, which keeps the reasoning about plan
shapes in one place and lets the harness's self-tests pin it against canned plans
without a database.

Two readings are offered:

* :func:`index_served_verdict` / :func:`assert_plan_is_index_served` — whether an
  index actually narrows every scan of the selection's own table (profile A,
  invariant 5);
* :func:`scan_counts` — rows returned against rows read and discarded by
  filters, from an ``EXPLAIN ANALYZE`` plan (the scan-ratio proof);
* :func:`read_cost` — buffers touched and rows visited, from an
  ``EXPLAIN (ANALYZE, BUFFERS)`` plan (the retained-history proof).
"""

import json
from collections.abc import Iterator
from typing import Any

from due_work_harness.host import ReadCost

#: Plan keys proving an index narrowed the rows a scan node returned.
PRUNING_KEYS = ("Index Cond", "Recheck Cond")


def iter_plan_nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Traverse a PostgreSQL plan once; every plan reading shares this JSON boundary."""
    yield plan
    for child in plan.get("Plans", ()):
        yield from iter_plan_nodes(child)


def _rendered(plan: dict[str, Any]) -> str:
    return f"Plan:\n{json.dumps(plan, indent=2)}"


def index_served_verdict(
    plan: dict[str, Any], *, table: str, predicate_indexes: frozenset[str] = frozenset()
) -> str | None:
    """
    ``None`` when an index narrows every scan of ``table``, else why not, with the plan.

    The reasoning behind each rule is on :func:`assert_plan_is_index_served`.
    ``predicate_indexes`` names partial indexes whose predicate a catalog-backed
    caller checked against this query; a bitmap built from one of them needs no
    ``Index Cond``, Filter above it or not. The plan cannot tell a Filter removing
    backlog (owed rows not due yet) from one removing settled history, so a named
    partial index is a claim the caller backs with
    :func:`~due_work_harness.profiles.automatic_recovery.assert_selection_cost_does_not_grow_with_the_history`.
    Names alone, or an arbitrary ``Recheck Cond``, never establish that guarantee.
    """
    scans = [node for node in iter_plan_nodes(plan) if node.get("Relation Name") == table]
    if not scans:
        return (
            f"no plan node scans {table!r}, so this proof cannot be evaluated. "
            f"Does the selection read the expected table?\n{_rendered(plan)}"
        )
    for node in scans:
        node_type = node.get("Node Type", "")
        if "Seq Scan" in node_type:
            return (
                f"the due-work selection falls back to a sequential scan of {table!r} even with "
                f"sequential scans disabled, so no usable index exists. This query runs on a "
                f"schedule forever.\n{_rendered(plan)}"
            )
        if node_type == "Bitmap Heap Scan":
            # A bitmap heap node always carries a Recheck Cond, whatever built
            # the bitmap, so reading it as evidence of pruning accepts a bitmap
            # assembled by scanning an entire index. The selectivity lives one
            # level down, on the Bitmap Index Scan.
            bitmap_scans = [scan for scan in iter_plan_nodes(node) if scan.get("Node Type") == "Bitmap Index Scan"]
            # Every input is read to build AND/OR bitmaps: one selective branch
            # cannot vouch for another that walks its entire unrelated index.
            if not bitmap_scans or not all(
                "Index Cond" in scan or scan.get("Index Name") in predicate_indexes for scan in bitmap_scans
            ):
                return (
                    f"the selection builds a bitmap over {table!r} from an index scan with no index "
                    f"condition, so the whole index is read and the heap rows are then filtered. The "
                    f"Recheck Cond on the heap node is not evidence of pruning — it is present for "
                    f"every bitmap. Add an index whose leading columns match the selection's "
                    f"predicate.\n{_rendered(plan)}"
                )
            continue
        # A scan with no Filter discards nothing: every row the index yields is
        # a result row. That is how a *partial* index serves a predicate — the
        # narrowing lives in the index's own WHERE clause, which EXPLAIN does
        # not surface as an Index Cond — and rejecting it would fail in the
        # accusing direction, which this proof treats as worse than silence.
        # The waste this invariant hunts is rows walked and then thrown away,
        # and that always shows up as a Filter.
        if "Filter" not in node:
            continue
        if not any(key in node for key in PRUNING_KEYS):
            return (
                f"the selection reaches an index on {table!r} ({node_type}) but no index condition "
                f"narrows it — every row is walked and filtered. That is a full scan by another name, "
                f"on a schedule. Add an index whose leading columns match the selection's "
                f"predicate.\n{_rendered(plan)}"
            )
    return None


def assert_plan_is_index_served(
    *, name: str, plan: dict[str, Any], table: str, predicate_indexes: frozenset[str] = frozenset()
) -> None:
    """
    The plan-shape verdict behind profile A's invariant 5, on an already-captured plan.

    A pure function of a plan tree, so the harness self-tests pin it against
    canned plans — including real plan shapes that once slipped past it —
    without needing a database or an adopter.

    Three false negatives motivated the current form, all found by pointing the
    proof at further adopters rather than by reasoning about it:

    1. *Absence of ``Seq Scan`` is not enough.* PostgreSQL will satisfy an
       unindexed predicate by walking an unrelated index end to end and
       filtering every row — a full scan wearing an index scan's name::

           Index Scan using unique_attempt_number_per_job
             Filter: ((updated_at <= ...) AND ((status = ANY (...)) OR ...))

    2. *A pruning marker anywhere in the tree is not enough.* In a join, the
       inner side's key lookup supplies an ``Index Cond`` while the driving scan
       still walks its whole table::

           Nested Loop Anti Join
             ->  Index Scan using orders_pkey                <- prunes nothing
                   Filter: (...)
             ->  Index Scan using shipments_order_id_idx     <- join key only
                   Index Cond: (order_id = orders.id)

    3. *A ``Recheck Cond`` is not a pruning key.* A bitmap heap node carries one
       unconditionally, whatever built the bitmap, so accepting it lets through a
       bitmap assembled by reading an entire index::

           Bitmap Heap Scan on job_attempts
             Recheck Cond: ...                     <- present for every bitmap
             Filter: (...)
             ->  Bitmap Index Scan using unique_attempt_number_per_job
                                                   <- no Index Cond: whole index

       It is the worst of the three, because the planner only chooses a bitmap
       once the table has rows — so the *same code* passed or failed depending
       on whether an earlier test in the same process had inserted any, and the
       order-dependent direction was toward green.

    And one false *positive*, found by writing a fix rather than an adopter:

    4. *A partial index carries no ``Index Cond`` for its own predicate.* When
       the index's WHERE clause subsumes the query's conditions, the plan is an
       index scan with no pruning key and no ``Filter`` — the narrowing lives in
       the index definition, invisible to the plan tree. Requiring a pruning key
       there rejects the best possible plan. A scan with no ``Filter`` discards
       nothing, so it is accepted: the waste this proof hunts is rows walked and
       then thrown away, and that always shows up as a ``Filter``.

    So the verdict inspects only the nodes scanning the selection's own table,
    requires each to be a non-sequential scan that either carries a pruning key
    or discards nothing, and — for a bitmap heap node — looks past its Recheck
    Cond to the Bitmap Index Scan that actually built the bitmap.
    """
    verdict = index_served_verdict(plan, table=table, predicate_indexes=predicate_indexes)
    assert verdict is None, f"{name}: {verdict}"


def scan_counts(plan: dict[str, Any]) -> tuple[float, float]:
    """
    ``(rows returned, rows discarded by filters)`` from an ``EXPLAIN ANALYZE`` plan.

    Returned is the largest ``Actual Rows`` of any node — the rows the selection
    actually produced before any limit; discarded is every ``Rows Removed by
    Filter`` and ``Rows Removed by Index Recheck`` in the tree, the rows read
    only to be thrown away. PostgreSQL reports each per loop, so a node run in a
    nested loop counts once per loop; a bitmap that reads a whole index and then
    rechecks it away shows up only as the recheck.
    """
    nodes = list(iter_plan_nodes(plan))
    returned = max(float(node.get("Actual Rows", 0) or 0) for node in nodes)
    discarded = sum(_removed(node) for node in nodes)
    return returned, discarded


def _loops(node: dict[str, Any]) -> float:
    return float(node.get("Actual Loops", 1) or 1)


def _removed_per_loop(node: dict[str, Any]) -> float:
    return float(node.get("Rows Removed by Filter", 0) or 0) + float(node.get("Rows Removed by Index Recheck", 0) or 0)


def _removed(node: dict[str, Any]) -> float:
    return _removed_per_loop(node) * _loops(node)


def read_cost(plan: dict[str, Any], *, table: str) -> ReadCost:
    """
    What the read cost, from the root of an ``EXPLAIN (ANALYZE, BUFFERS)`` plan.

    ``blocks`` is the root's inclusive shared-buffer count, so each block is
    counted once however many nodes touched it. ``visited`` is every row the
    scans of ``table`` produced or discarded, over all their loops: the work an
    index that reads its whole range and filters it leaves behind, which
    ``blocks`` alone would blur on a small table.
    """
    scans = [node for node in iter_plan_nodes(plan) if node.get("Relation Name") == table]
    assert scans, f"the read never scanned {table!r}, so its cost says nothing about that table"
    visited = sum((float(node.get("Actual Rows", 0) or 0) + _removed_per_loop(node)) * _loops(node) for node in scans)
    return ReadCost(
        blocks=int(plan.get("Shared Hit Blocks", 0)) + int(plan.get("Shared Read Blocks", 0)),
        visited=visited,
        sequential=any("Seq Scan" in node.get("Node Type", "") for node in scans),
    )
