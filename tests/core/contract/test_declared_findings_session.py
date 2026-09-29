"""
Declared findings in a real session: a finding that moved fails, and is never passed off as the known gap.

The strict xfail of a gap on a history with findings accepts only
``HistoriesDiverged``; these child sessions show pytest reporting the table's
own failure as a failure, and the pinned divergence as the xfail.
"""

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from tests.core.contract.child_suites import SUITE


def _outcome(tmp_path: Path, pinned: str) -> str:
    (tmp_path / "test_split.py").write_text(dedent(SUITE.format(pinned=pinned)), encoding="utf-8")
    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "handoff", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )
    return child.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize(
    ("pinned", "reported"),
    [("worker died after commit 1", "1 xfailed"), ("worker died after commit 2", "1 failed")],
    ids=["as-pinned", "moved"],
)
def test_only_the_pinned_divergence_is_the_known_gap(tmp_path: Path, pinned: str, reported: str) -> None:
    assert reported in _outcome(tmp_path, pinned)
