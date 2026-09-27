from adopter_app import summaries

from due_work_harness import DueWorkSource, LossIsAbsorbedElsewhere, exempt_due_work_suite


def _strand() -> int:
    # ARRANGE: the order was renamed and its eviction was lost, so the old summary is still cached.
    summaries.ORDERS[1], summaries.SUMMARIES[1] = "renamed", "original"
    return 1


@exempt_due_work_suite(
    DueWorkSource(summaries.rename_order),
    reason="the read path rebuilds a summary that disagrees with its order, so a lost eviction costs nothing",
    prove=LossIsAbsorbedElsewhere(
        strand=_strand,
        observe=lambda order_id: summaries.SUMMARIES.get(order_id) == summaries.ORDERS[order_id],
        absorb=summaries.summary,
    ),
)
class TestSummaryEvictionExemption:
    pass
