"""
Invariant 5's plan verdict and the scan counts, against canned PostgreSQL plans.

Pure functions over the plan tree ``EXPLAIN (FORMAT JSON)`` returns, so no
database is needed. The three plan shapes that once slipped past the verdict as
false negatives, and the one it once wrongly rejected, are pinned here as data,
so they can never again depend on which selection happens to be checked first.
"""

import pytest

from due_work_harness.host import ReadCost
from due_work_harness.integrations.postgres_plans import (
    assert_plan_is_index_served,
    bitmap_overturns_the_walk,
    index_served_verdict,
    iter_plan_nodes,
    read_cost,
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


def test_rows_a_bitmap_recheck_discards_are_counted() -> None:
    """A bitmap that reads a whole index and rechecks it away reports no Filter at all."""
    plan = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Actual Rows": 5,
        "Rows Removed by Index Recheck": 995,
        "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "some_index", "Actual Rows": 1000}],
    }
    assert scan_counts(plan) == (1000.0, 995.0)


def test_scan_counts_multiply_the_per_loop_figures_by_the_loops() -> None:
    plan = {
        "Node Type": "Nested Loop",
        "Actual Rows": 10,
        "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "shop_driver", "Actual Rows": 100, "Actual Loops": 1},
            _index_scan(**{"Actual Rows": 0, "Actual Loops": 100, "Rows Removed by Filter": 3}),
        ],
    }
    assert scan_counts(plan) == (100.0, 300.0)


def test_read_cost_is_buffers_at_the_root_and_rows_visited_over_every_loop() -> None:
    plan = {
        "Node Type": "Nested Loop",
        "Shared Hit Blocks": 5,
        "Shared Read Blocks": 2,
        "Plans": [
            {"Node Type": "Index Scan", "Relation Name": "shop_driver", "Actual Rows": 100, "Actual Loops": 1},
            _index_scan(**{"Actual Rows": 1, "Actual Loops": 100, "Rows Removed by Filter": 3}),
        ],
    }
    # Only the selection's own table is counted: (1 produced + 3 discarded) x 100 loops.
    assert read_cost(plan, table=_TABLE) == ReadCost(blocks=7, visited=400.0, sequential=False)


@pytest.mark.parametrize("node_type", ["Seq Scan", "Parallel Seq Scan"])
def test_a_sequential_scan_of_the_table_is_reported(node_type: str) -> None:
    plan = {"Node Type": node_type, "Relation Name": _TABLE, "Actual Rows": 3, "Shared Hit Blocks": 40}
    assert read_cost(plan, table=_TABLE) == ReadCost(blocks=40, visited=3.0, sequential=True)


def test_a_read_that_never_scans_the_table_says_nothing_about_it() -> None:
    with pytest.raises(AssertionError, match="never scanned 'shop_examplework'"):
        read_cost({"Node Type": "Result"}, table=_TABLE)


def test_a_named_partial_index_is_trusted_with_a_filter_on_other_columns() -> None:
    """
    A named partial index vouches for its bitmap when the Filter above it tests columns its predicate does not.

    ``(id) WHERE status IN (active)`` under a time-bounded selection: the Filter on
    ``updated_at`` removes owed rows that are not due yet, backlog the predicate
    rightly keeps. What the plan cannot show (how much settled history the index
    holds) the retained-history proof measures.
    """
    plan = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Recheck Cond": "(status = ANY ('{requested,running}'))",
        "Filter": "(updated_at <= $1)",
        "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "active_ix"}],
    }
    assert_plan_is_index_served(
        name="partial bitmap", plan=plan, table=_TABLE, predicate_indexes={"active_ix": frozenset({"status"})}
    )
    with pytest.raises(AssertionError, match="Recheck Cond .* is not evidence"):
        assert_plan_is_index_served(name="partial bitmap", plan=plan, table=_TABLE)


@pytest.mark.parametrize(
    "scan",
    [
        {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": _TABLE,
            "Recheck Cond": "(status IS NOT NULL)",
            "Filter": "(((status)::text = ANY ('{requested,running}'::text[])) AND (updated_at <= $1))",
            "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "weak_ix"}],
        },
        {
            "Node Type": "Index Scan",
            "Relation Name": _TABLE,
            "Index Name": "weak_ix",
            "Index Cond": "(updated_at <= $1)",
            "Filter": "((status)::text = ANY ('{requested,running}'::text[]))",
        },
    ],
    ids=["bitmap", "index-scan"],
)
def test_a_named_partial_index_whose_filter_re_tests_its_predicate_is_refused(scan: dict[str, object]) -> None:
    # The Filter re-tests status, which the predicate constrains: the predicate does not imply the selection's
    # own condition there, so the index keeps rows the selection throws away (``status IS NOT NULL`` keeps the
    # whole settled history).
    with pytest.raises(AssertionError, match=r"weak_ix.*constrains \['status'\].*filters on them again"):
        assert_plan_is_index_served(
            name="weak partial", plan=scan, table=_TABLE, predicate_indexes={"weak_ix": frozenset({"status"})}
        )


def test_a_literal_in_the_filter_is_not_a_column() -> None:
    plan = {
        "Node Type": "Index Scan",
        "Relation Name": _TABLE,
        "Index Name": "active_ix",
        "Index Cond": "(updated_at <= $1)",
        "Filter": "((kind)::text = 'status')",
    }
    assert_plan_is_index_served(
        name="literal", plan=plan, table=_TABLE, predicate_indexes={"active_ix": frozenset({"status"})}
    )


# On a tiny table every index walk costs about the same, so with sequential scans off the planner
# may walk an unrelated index (a unique key) and filter the due-time bound. The bitmap-only plan
# then decides: a bitmap needs an index condition, or a named partial predicate, to narrow at all.

_WALKS_AN_UNRELATED_INDEX = _index_scan(Filter="((state = 'ready') AND (available_at <= $1))")
_BITMAP_OVER_THE_DUE_INDEX = {
    "Node Type": "Bitmap Heap Scan",
    "Relation Name": _TABLE,
    "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "due_ix", "Index Cond": "(available_at <= $1)"}],
}
_BITMAP_OVER_A_STATE_INDEX_READING_EVERY_STATE = {
    "Node Type": "Bitmap Heap Scan",
    "Relation Name": _TABLE,
    "Recheck Cond": "(state = ANY ('{ready,queued,claimed,retryable,unknown}'::text[]))",
    "Filter": "(available_at <= $1)",
    "Plans": [
        {
            "Node Type": "Bitmap Index Scan",
            "Index Name": "state_ix",
            "Index Cond": "(state = ANY ('{ready,queued,claimed,retryable,unknown}'::text[]))",
        }
    ],
}
_BITMAP_WITHOUT_AN_INDEX_CONDITION = {
    "Node Type": "Bitmap Heap Scan",
    "Relation Name": _TABLE,
    "Recheck Cond": "(state = 'ready')",
    "Filter": "(available_at <= $1)",
    "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "some_index"}],
}


@pytest.mark.parametrize(
    ("bitmap", "overturns"),
    [
        (_BITMAP_OVER_THE_DUE_INDEX, True),
        (_WALKS_AN_UNRELATED_INDEX, False),
        # A bitmap is not narrowing by being a bitmap: this one reads the whole state index and
        # still filters the due-time bound the walked scan filtered.
        (_BITMAP_OVER_A_STATE_INDEX_READING_EVERY_STATE, False),
        (_BITMAP_WITHOUT_AN_INDEX_CONDITION, False),
    ],
    ids=["a-narrowing-index-exists", "no-narrowing-index", "bitmap-leaves-the-due-bound-unindexed", "no-index-cond"],
)
def test_a_tiny_table_walking_an_unrelated_index_is_judged_by_the_bitmap_plan(
    bitmap: dict[str, object], overturns: bool
) -> None:
    assert index_served_verdict(_WALKS_AN_UNRELATED_INDEX, table=_TABLE) is not None
    assert bitmap_overturns_the_walk(_WALKS_AN_UNRELATED_INDEX, bitmap, table=_TABLE) is overturns


def test_a_walk_that_filters_no_range_bound_is_not_overturned() -> None:
    # Only a range bound on a tiny table makes the walk a costing artifact; an equality filter is no excuse.
    walked = _index_scan(Filter="(state = 'ready')")
    bitmap = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "state_ix", "Index Cond": "(state = 'ready')"}],
    }
    assert bitmap_overturns_the_walk(walked, bitmap, table=_TABLE) is False


def test_the_bitmap_is_judged_with_the_named_partial_indexes() -> None:
    # A named partial index needs no Index Cond, for the verdict and the fallback alike; it must be named.
    bitmap = {
        "Node Type": "Bitmap Heap Scan",
        "Relation Name": _TABLE,
        "Plans": [
            {"Node Type": "Bitmap Index Scan", "Index Name": "ready_due_ix", "Index Cond": "(available_at <= $1)"}
        ],
    }
    named = {"ready_due_ix": frozenset({"state"})}
    assert bitmap_overturns_the_walk(_WALKS_AN_UNRELATED_INDEX, bitmap, table=_TABLE, predicate_indexes=named)
    unbounded = {**bitmap, "Plans": [{"Node Type": "Bitmap Index Scan", "Index Name": "ready_due_ix"}]}
    assert bitmap_overturns_the_walk(_WALKS_AN_UNRELATED_INDEX, unbounded, table=_TABLE, predicate_indexes=named)
    assert not bitmap_overturns_the_walk(_WALKS_AN_UNRELATED_INDEX, unbounded, table=_TABLE)


def test_a_plain_scan_on_a_named_partial_index_passes_when_its_filter_leaves_the_predicate_alone() -> None:
    # The partial index holds only the owed states; the Filter drops the backlog not yet due. Same rule as a
    # bitmap over a named partial index: the Filter must not re-test a column the predicate constrains.
    named = {"owed_ix": frozenset({"status"})}
    backlog = _index_scan(**{"Index Name": "owed_ix", "Filter": "(updated_at <= $1)"})
    assert index_served_verdict(backlog, table=_TABLE, predicate_indexes=named) is None
    assert index_served_verdict(backlog, table=_TABLE) is not None
    re_tested = _index_scan(**{"Index Name": "owed_ix", "Filter": "((status = 'ready') AND (updated_at <= $1))"})
    assert "filters on them again" in (index_served_verdict(re_tested, table=_TABLE, predicate_indexes=named) or "")


def _bitmap(*inputs: dict[str, object], combine: str | None = None, **heap: object) -> dict[str, object]:
    plans = [{"Node Type": combine, "Plans": list(inputs)}] if combine else list(inputs)
    return {"Node Type": "Bitmap Heap Scan", "Relation Name": _TABLE, "Plans": plans, **heap}


def _input(index: str, cond: str | None = None) -> dict[str, object]:
    return {"Node Type": "Bitmap Index Scan", "Index Name": index, **({"Index Cond": cond} if cond else {})}


@pytest.mark.parametrize(
    ("bitmap", "overturns"),
    [
        # Each arm of an OR is read on its own: the state arm reads every ready row and still filters the due time.
        (
            _bitmap(
                _input("due_ix", "(available_at <= $1)"),
                _input("state_ix", "(state = 'ready')"),
                combine="BitmapOr",
                Filter="(available_at <= $1)",
            ),
            False,
        ),
        (
            _bitmap(
                _input("due_ix", "(available_at <= $1)"),
                _input("due_state_ix", "((state = 'ready') AND (available_at <= $1))"),
                combine="BitmapOr",
            ),
            True,
        ),
        # An AND is narrowed by every arm together.
        (
            _bitmap(
                _input("due_ix", "(available_at <= $1)"), _input("state_ix", "(state = 'ready')"), combine="BitmapAnd"
            ),
            True,
        ),
        # A column named inside a string literal is not indexed.
        (_bitmap(_input("kind_ix", "(kind = 'available_at <= soon')"), Filter="(available_at <= $1)"), False),
    ],
    ids=["or-arm-leaves-the-bound", "or-arms-each-bound", "and-arms-together", "literal-is-not-a-column"],
)
def test_every_arm_the_bitmap_reads_must_narrow_the_bound(bitmap: dict[str, object], overturns: bool) -> None:
    assert bitmap_overturns_the_walk(_WALKS_AN_UNRELATED_INDEX, bitmap, table=_TABLE) is overturns


@pytest.mark.parametrize(
    "bound",
    [
        "('2026-01-01 00:00:00+00'::timestamp with time zone >= available_at)",
        "(available_at <= (now() - '00:05:00'::interval))",
        "((available_at)::timestamp without time zone <= $1)",
    ],
    ids=["literal-on-the-left", "function-on-the-right", "cast-column"],
)
def test_the_range_bounded_column_is_read_on_either_side_of_its_operator(bound: str) -> None:
    walked = _index_scan(Filter=f"((state = 'ready') AND {bound})")
    assert bitmap_overturns_the_walk(walked, _BITMAP_OVER_THE_DUE_INDEX, table=_TABLE)
    assert not bitmap_overturns_the_walk(walked, _BITMAP_OVER_A_STATE_INDEX_READING_EVERY_STATE, table=_TABLE)


def test_a_bitmap_only_of_named_partial_indexes_overturns_the_walk() -> None:
    # The verdict accepts it (the predicate narrows; the Filter drops only backlog), so the fallback must too.
    named = {"owed_ix": frozenset({"state"})}
    bitmap = _bitmap(_input("owed_ix"), Filter="(available_at <= $1)")
    assert index_served_verdict(bitmap, table=_TABLE, predicate_indexes=named) is None
    assert bitmap_overturns_the_walk(_WALKS_AN_UNRELATED_INDEX, bitmap, table=_TABLE, predicate_indexes=named)
    assert not bitmap_overturns_the_walk(_WALKS_AN_UNRELATED_INDEX, bitmap, table=_TABLE)
