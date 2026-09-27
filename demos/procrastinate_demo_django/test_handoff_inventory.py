"""
The coverage scan on procrastinate's Django demo: it finds both handoffs the demo makes.

The first is the one ``test_demo_django.py`` shows losing a book: the create
view defers ``index_book`` after the book commits. The scan is what makes a
project notice such a handoff before anyone writes its crash history.
"""

from pathlib import Path

from due_work_harness.coverage import CoverageConfig, production_sites
from due_work_harness.coverage.sites import PROCRASTINATE

UPSTREAM = Path(__file__).resolve().parents[1] / ".upstream" / "procrastinate"
DEMO = "procrastinate.demos.demo_django"


def test_the_scan_finds_every_handoff_the_demo_makes() -> None:
    config = CoverageConfig(root=UPSTREAM, production_packages=(DEMO,), source_roots=(".",), kinds=(PROCRASTINATE,))
    sites = {name: [(site.kind, site.path) for site in found] for name, found in production_sites(config).items()}
    assert sites == {
        f"{DEMO}.demo.views.CreateBookView.form_valid": [
            ("procrastinate", "procrastinate/demos/demo_django/demo/views.py")
        ],
        f"{DEMO}.demo.tasks.index_book": [("procrastinate", "procrastinate/demos/demo_django/demo/tasks.py")],
    }
