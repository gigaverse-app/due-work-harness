"""
Every handoff the demo makes is accounted for, as ``due-work-harness check`` requires.

The scan finds both: the create view's ``.defer``, which ``DEMO_AS_SHIPPED``
covers, and ``index_book``'s, which the baseline in ``pyproject.toml`` holds.
"""

from pathlib import Path

from due_work_harness.coverage import assert_every_site_is_accounted_for

HERE = Path(__file__).resolve().parent


def test_every_handoff_the_demo_makes_is_accounted_for() -> None:
    report = assert_every_site_is_accounted_for(HERE)
    assert {name: disposition.how for name, disposition in report.dispositions.items()} == {
        "procrastinate.demos.demo_django.demo.views.CreateBookView.form_valid": "covered",
        "procrastinate.demos.demo_django.demo.tasks.index_book": "baseline",
    }
