"""
The coverage scan on DBOS's transactional-outbox demo, including what it cannot see.

``atomic_workflow.py`` starts its workflow with ``DBOS.start_workflow`` and the
scan finds it. ``transactional_enqueue.py`` enqueues by calling DBOS's
``enqueue_workflow`` SQL function inside the order's transaction: a handoff made
in SQL is invisible to a static scan, which is why that demo's contract is a
crash history (``test_transactional_outbox.py``) rather than a scan finding.
"""

from pathlib import Path

from due_work_harness.coverage import CoverageConfig, production_sites
from due_work_harness.coverage.sites import DBOS

DEMO = Path(__file__).resolve().parents[1] / ".upstream" / "dbos-demo-apps" / "python" / "transactional-outbox"


def test_the_scan_finds_the_started_workflow_and_not_the_sql_enqueue() -> None:
    config = CoverageConfig(
        root=DEMO, production_packages=("atomic_workflow", "transactional_enqueue"), source_roots=(".",), kinds=(DBOS,)
    )
    sites = {name: [site.kind for site in found] for name, found in production_sites(config).items()}
    assert sites == {"atomic_workflow.create_order": ["dbos"]}
