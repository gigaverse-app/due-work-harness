"""
``pytest --due-work-summary``: what each generated suite produced, listed after the run.

A generated suite's class is empty in the source; the summary is where a reader
sees its cases, their outcomes and each declared gap's reason.
"""

import os
import subprocess
import sys
from pathlib import Path

from .builders import write_project

ORDERS = """
    SUMMARIES: set[int] = set()

    def rename(order_id):
        SUMMARIES.discard(order_id)

    def summary(order_id):
        SUMMARIES.add(order_id)

    def has_summary(order_id):
        return order_id in SUMMARIES
"""

SUITE = """
    from due_work_harness import DueWorkSource, LossIsAbsorbedElsewhere, exempt_due_work_suite
    from shop import orders

    @exempt_due_work_suite(
        DueWorkSource(orders.rename),
        reason="the read path rebuilds a summary the lost eviction left stale",
        prove=LossIsAbsorbedElsewhere(strand=lambda: 1, observe=orders.has_summary, absorb=orders.summary),
    )
    class TestRenameExemption:
        pass

    def test_unrelated():
        pass
"""


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(root), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )


def test_the_summary_lists_each_generated_case_by_suite(tmp_path: Path) -> None:
    write_project(
        tmp_path,
        {"shop/orders.py": ORDERS, "tests/test_rename.py": SUITE},
        extra='\n[tool.pytest.ini_options]\npythonpath = ["."]\n',
    )
    completed = _run(tmp_path, "--due-work-summary")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = completed.stdout.split("due-work-harness: what each suite generated", 1)[1]
    assert "tests/test_rename.py::TestRenameExemption:" in summary
    assert "  PASSED  " in summary
    # A hand-written test is not a generated case.
    assert "test_unrelated" not in summary


def test_without_the_option_nothing_is_listed(tmp_path: Path) -> None:
    write_project(
        tmp_path,
        {"shop/orders.py": ORDERS, "tests/test_rename.py": SUITE},
        extra='\n[tool.pytest.ini_options]\npythonpath = ["."]\n',
    )
    assert "what each suite generated" not in _run(tmp_path).stdout
