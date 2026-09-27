"""
Order summaries: the authoritative orders, and a derived cache of their summaries.

Renaming an order evicts its cached summary after the commit. That eviction is
a handoff — if the process dies right after the commit, it never happens — and
this module is written so losing it costs nothing: the read path compares the
cached summary with the order and rebuilds it when they disagree. The adopter's
exemption (``tests/test_summaries_exemption.py``) states that and proves it.
"""

ORDERS: dict[int, str] = {}
SUMMARIES: dict[int, str] = {}


def rename_order(order_id: int, name: str) -> None:
    from django.db import transaction

    ORDERS[order_id] = name
    transaction.on_commit(lambda: SUMMARIES.pop(order_id, None))


def summary(order_id: int) -> str:
    """The order's summary, rebuilt from the order whenever the cached one is stale."""
    if SUMMARIES.get(order_id) != ORDERS[order_id]:
        SUMMARIES[order_id] = ORDERS[order_id]
    return SUMMARIES[order_id]
