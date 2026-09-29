"""
``pytest --due-work-record-findings``: the findings a session would pin, printed instead of checked.

The reference suite declares a table that is out of date (it pins the wrong commit), which would fail
a normal run. Recording prints what the code does today, ready to paste, and ignores the declared table.
"""

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from tests.core.contract.test_declared_findings_session import SUITE


def _run(tmp_path: Path, *options: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_split.py").write_text(dedent(SUITE.format(pinned="worker died after commit 2")), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "handoff", str(tmp_path), *options],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )


def test_a_stale_table_fails_a_normal_run(tmp_path: Path) -> None:
    assert "1 failed" in _run(tmp_path).stdout


def test_recording_prints_the_findings_and_ignores_the_stale_table(tmp_path: Path) -> None:
    completed = _run(tmp_path, "--due-work-record-findings")
    assert "1 xfailed" in completed.stdout, completed.stdout
    recorded = completed.stdout.split("recorded findings", 1)[1]
    assert "Findings(" in recorded
    assert "('retryable_failed', ('running',))," in recorded
    assert "'worker died after commit 1': ('retryable_failed', ())" in recorded
