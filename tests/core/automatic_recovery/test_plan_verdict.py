"""
Invariant 5's plan verdict and the scan counts, against canned PostgreSQL plans.

Pure functions over the plan tree ``EXPLAIN (FORMAT JSON)`` returns, so no
database is needed. The three plan shapes that once slipped past the verdict as
false negatives, and the one it once wrongly rejected, are pinned here as data,
so they can never again depend on which selection happens to be checked first.
"""

import pytest

from due_work_harness.integrations.postgres_plans import (
    assert_plan_is_index_served,
    index_served_verdict,
    iter_plan_nodes,
    scan_counts,
)

_TABLE = "shop_examplework"


def _index_scan(**extra: object) -> dict[str, object]:
    return {"Node Type": "Index Scan", "Relation Name": _TABLE, "Index Name": "some_index", **extra}


def test_a_genuinely_pruning_index_scan_passes() -> None:
    assert_plan_is_index_served(
        name="pruning index scan", plan=_index_scan(**{"Index Cond": "(created_at <= $1)"}), table=_TABLE
    )


def test_a_sequential_scan_fails() -> None:
    with pytest.raises(AssertionError, match="sequential\\s+scan"):
        assert_plan_is_index_served(
            name="seq scan", plan={"Node Type": "Seq Scan", "Relation Name": _TABLE}, table=_TABLE
        )


def test_a_parallel_sequential_scan_fails() -> None:
    plan = {"Node Type": "Gather", "Plans": [{"Node Type": "Parallel Seq Scan", "Relation Name": _TABLE}]}
    with pytest.raises(AssertionError, match="sequential\\s+scan"):
        assert_plan_is_index_served(name="parallel seq scan", plan=plan, table=_TABLE)


def test_false_negative_1_a_full_scan_wearing_an_index_scans_name_fails() -> None:
    """An index scan with everything in Filter and no Index Cond walks the whole index."""
    with pytest.raises(AssertionError, match="full scan by another name"):
        assert_plan_is_index_served(
            name="filter-only index scan", plan=_index_scan(Filter="(status = ANY ('{RUNNING}'))"), table=_TABLE
        )


def test_false_negative_2_an_inner_joins_index_cond_does_not_vouch_for_the_driving_scan() -> None:
    """
    The anti-join shape: the inner side carries an Index Cond on the join key
    while the driving scan walks its whole table.
    """
    plan = {
        "Node Type": "Nested Loop Anti Join",
        "Plans": [
            _index_scan(Filter="(status <> 'CANCELLED')"),
            {
                "Node Type": "Index Scan",
                "Relation Name": "shop_othertable",
                "Index Cond": f"(event_id = {_TABLE}.id)",
            },
        ],
    }
    with pytest.raises(AssertionError, match="full scan by another name"):
        assert_plan_is_index_served(name="anti-join", plan=plan, table=_TABLE)


def test_false_negative_3_a_recheck_cond_is_not_a_pruning_key() -> None:
    """
    The bitmap shape: every bitmap heap node carries a Recheck Cond, so the
    verdict must look down to the Bitmap Index Scan that built the bitmap.
    """
    plan = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Recheck Cond": "(status = ANY ('{RUNNING}'))",
        # No Index Cond on the Bitmap Index Scan: the whole index was read.
        "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "some_unrelated_unique_index"}],
    }
    with pytest.raises(AssertionError, match="Recheck Cond .* is not evidence"):
        assert_plan_is_index_served(name="unpruned bitmap", plan=plan, table=_TABLE)


def test_false_positive_4_a_partial_index_scan_with_no_filter_passes() -> None:
    """
    A partial index serving the whole predicate yields a plan with neither an
    Index Cond nor a Filter — the narrowing lives in the index's own WHERE
    clause. A scan that discards nothing must be accepted.
    """
    assert_plan_is_index_served(name="partial index scan", plan=_index_scan(), table=_TABLE)


@pytest.mark.parametrize("verified", [True, False])
def test_partial_bitmap_needs_catalog_predicate_evidence(verified: bool) -> None:
    plan = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Recheck Cond": "state = 'ready'",
        "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "ready_ix"}],
    }

    def verify() -> None:
        assert_plan_is_index_served(
            name="partial bitmap",
            plan=plan,
            table=_TABLE,
            predicate_indexes=frozenset({"ready_ix"} if verified else {"unrelated_ix"}),
        )

    if verified:
        verify()
    else:
        with pytest.raises(AssertionError, match="Recheck Cond .* is not evidence"):
            verify()


def test_a_genuinely_pruning_bitmap_passes() -> None:
    plan = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Recheck Cond": "(created_at <= $1)",
        "Plans": [
            {"Node Type": "Bitmap Index Scan", "Index Name": "created_at_ix", "Index Cond": "(created_at <= $1)"}
        ],
    }
    assert_plan_is_index_served(name="pruning bitmap", plan=plan, table=_TABLE)


def test_a_bitmap_or_of_pruning_partial_indexes_passes() -> None:
    """An OR served by BitmapOr over two indexes prunes: each branch carries its own Index Cond."""
    plan = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Recheck Cond": "((a < 5) OR (b <= $1))",
        "Plans": [
            {
                "Node Type": "BitmapOr",
                "Plans": [
                    {"Node Type": "Bitmap Index Scan", "Index Name": "partial_a_ix", "Index Cond": "(a < 5)"},
                    {"Node Type": "Bitmap Index Scan", "Index Name": "partial_b_ix", "Index Cond": "(b <= $1)"},
                ],
            }
        ],
    }
    assert_plan_is_index_served(name="bitmap-or", plan=plan, table=_TABLE)


@pytest.mark.parametrize("operator", ["BitmapOr", "BitmapAnd"])
@pytest.mark.parametrize("verified", [False, True], ids=["unbounded-branch", "verified-partial-branch"])
def test_every_bitmap_input_must_have_pruning_evidence(operator: str, verified: bool) -> None:
    plan = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Recheck Cond": "(created_at < now() OR state = 'ready')",
        "Plans": [
            {
                "Node Type": operator,
                "Plans": [
                    {"Node Type": "Bitmap Index Scan", "Index Name": "date_ix", "Index Cond": "created_at < now()"},
                    {"Node Type": "Bitmap Index Scan", "Index Name": "ready_ix"},
                ],
            }
        ],
    }

    def verify() -> None:
        assert_plan_is_index_served(
            name="mixed bitmap",
            plan=plan,
            table=_TABLE,
            predicate_indexes=frozenset({"ready_ix"} if verified else ()),
        )

    if verified:
        verify()
    else:
        with pytest.raises(AssertionError, match="Recheck Cond .* is not evidence"):
            verify()


def test_a_plan_that_never_scans_the_table_is_unevaluable_not_green() -> None:
    with pytest.raises(AssertionError, match="no plan node scans"):
        assert_plan_is_index_served(
            name="wrong table", plan=_index_scan(**{"Index Cond": "(x = 1)", "Relation Name": "other"}), table=_TABLE
        )


def test_the_verdict_is_none_when_served_and_carries_the_plan_when_not() -> None:
    assert index_served_verdict(_index_scan(**{"Index Cond": "(x = 1)"}), table=_TABLE) is None
    verdict = index_served_verdict({"Node Type": "Seq Scan", "Relation Name": _TABLE}, table=_TABLE)
    assert verdict is not None and '"Node Type": "Seq Scan"' in verdict


def test_the_plan_walk_visits_every_node_once() -> None:
    plan = {"Node Type": "Limit", "Plans": [{"Node Type": "Sort", "Plans": [_index_scan()]}]}
    assert [node["Node Type"] for node in iter_plan_nodes(plan)] == ["Limit", "Sort", "Index Scan"]


def test_scan_counts_are_rows_produced_against_rows_filtered_away() -> None:
    """Returned is the largest node's rows before any limit; discarded sums every filter in the tree."""
    plan = {
        "Node Type": "Limit",
        "Actual Rows": 5,
        "Plans": [
            {
                "Node Type": "Nested Loop",
                "Actual Rows": 40,
                "Plans": [
                    {**_index_scan(Filter="(status = 'due')"), "Actual Rows": 40, "Rows Removed by Filter": 360},
                    {"Node Type": "Index Scan", "Relation Name": "shop_othertable", "Rows Removed by Filter": 2},
                ],
            }
        ],
    }
    assert scan_counts(plan) == (40.0, 362.0)
    assert scan_counts({"Node Type": "Result"}) == (0.0, 0.0)
