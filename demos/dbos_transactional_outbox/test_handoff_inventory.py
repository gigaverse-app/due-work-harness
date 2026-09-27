"""
Every handoff the demo makes is accounted for, as ``due-work-harness check`` requires, and what the scan cannot see.

``atomic_workflow.py`` starts its workflow with ``DBOS.start_workflow``: the
scan finds it, and the baseline holds it. ``transactional_enqueue.py``
enqueues by calling DBOS's ``enqueue_workflow`` SQL function inside the
order's transaction: a handoff made in SQL is invisible to a static scan,
which is why ``PLACE_ORDER_CONTRACT`` stands on its crash histories and
covers no site.
"""

from pathlib import Path

from due_work_harness.coverage import assert_every_site_is_accounted_for

HERE = Path(__file__).resolve().parent


def test_the_scan_finds_the_started_workflow_and_not_the_sql_enqueue() -> None:
    report = assert_every_site_is_accounted_for(HERE)
    assert {name: disposition.how for name, disposition in report.dispositions.items()} == {
        "atomic_workflow.create_order": "baseline"
    }
    assert list(report.sites) == ["atomic_workflow.create_order"]
