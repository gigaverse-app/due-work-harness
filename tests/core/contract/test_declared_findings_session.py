"""
Declared findings in a real session: a finding that moved fails, and is never passed off as the known gap.

The strict xfail of a gap on a history with findings accepts only
``HistoriesDiverged``; these child sessions show pytest reporting the table's
own failure as a failure, and the pinned divergence as the xfail.
"""

from pathlib import Path

import pytest

from tests.core.contract.child_suites import run_child_session


def _outcome(directory: Path, pinned: str) -> str:
    stdout = run_child_session(directory, pinned).stdout.strip()
    return stdout.splitlines()[-1] if stdout else "<the child printed nothing>"


@pytest.mark.parametrize(
    ("pinned", "reported"),
    [("worker died after commit 1", "1 xfailed"), ("worker died after commit 2", "1 failed")],
    ids=["as-pinned", "moved"],
)
def test_only_the_pinned_divergence_is_the_known_gap(tmp_path: Path, pinned: str, reported: str) -> None:
    assert reported in _outcome(tmp_path, pinned)


def test_a_child_session_ignores_configuration_above_its_directory(tmp_path: Path) -> None:
    # A configuration file in an ancestor of the child's directory makes that ancestor the child's rootdir:
    # pytest then reads its options, loads its conftest, and scans it while collecting. On a developer
    # machine the ancestor is the shared temporary directory, where other processes create and delete
    # entries while the child scans it. The child must be rooted in its own directory whatever lies above.
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --an-option-the-child-must-never-see\n", "utf-8")
    (tmp_path / "conftest.py").write_text("raise RuntimeError('an ancestor conftest was loaded')\n", "utf-8")
    session = tmp_path / "session"
    session.mkdir()
    assert "1 xfailed" in _outcome(session, "worker died after commit 1")
