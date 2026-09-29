"""
``pytest --due-work-record-findings``: the findings a session would pin, printed instead of checked.

The reference suite declares a table that is out of date (it pins the wrong commit), which would fail
a normal run. Recording prints what the code does today, ready to paste, and ignores the declared table.
"""

import subprocess
from pathlib import Path

from tests.core.contract.child_suites import run_child_session


def _run(tmp_path: Path, *options: str) -> subprocess.CompletedProcess[str]:
    return run_child_session(tmp_path, "worker died after commit 2", *options)


def test_a_stale_table_fails_a_normal_run(tmp_path: Path) -> None:
    assert "1 failed" in _run(tmp_path).stdout


def test_recording_prints_the_findings_and_ignores_the_stale_table(tmp_path: Path) -> None:
    completed = _run(tmp_path, "--due-work-record-findings")
    assert "1 xfailed" in completed.stdout, completed.stdout
    recorded = completed.stdout.split("recorded findings", 1)[1]
    assert "Findings(" in recorded
    assert "('retryable_failed', ('running',))," in recorded
    assert "'worker died after commit 1': ('retryable_failed', ())" in recorded


def test_recording_under_xdist_is_refused_instead_of_reporting_nothing(tmp_path: Path) -> None:
    completed = _run(tmp_path, "--due-work-record-findings", "-n", "2")

    assert completed.returncode != 0
    assert "run without -n" in completed.stderr + completed.stdout
