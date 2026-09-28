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

SUITE = """
    from due_work_harness import Findings, configure
    from due_work_harness.contract import Adoption, NotApplicable, Profile, SafetyContract, SafetyProfile
    from due_work_harness.contract import DueWorkContract, due_work_contract_suite
    from due_work_harness.crash_histories import HandoffHistory
    from due_work_harness.host import Host
    from due_work_harness.references import in_memory_handoffs as ref

    configure(Host(worker_killer=ref.ledger_killer))
    WHY = "the successor is committed separately"
    NA = NotApplicable("self-test")

    CONTRACT = DueWorkContract(
        name="split handoff",
        adoption=Adoption.LEGACY,
        profiles={{profile: NA for profile in Profile}},
        safety=SafetyContract(name="split handoff", profiles={{profile: NA for profile in SafetyProfile}}),
        handoffs=(
            HandoffHistory(
                name="retryable failure",
                arrange=ref.running_attempt,
                transition=ref.fail_with_split_handoff,
                observe=ref.attempt_and_successors,
                findings=Findings(("retryable_failed", ("running",)), {{{pinned!r}: ("retryable_failed", ())}}),
            ),
        ),
        handoff_delivery=ref.RETRY_DELIVERY,
        handoff_gaps={{"retryable failure": WHY}},
    )

    @due_work_contract_suite(CONTRACT)
    class TestSplit:
        pass
"""


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
