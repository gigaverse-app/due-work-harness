"""Source for the child pytest sessions that the findings tests run, and how they are run."""

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

SUITE = """
    from pytest_obligation import Findings, configure
    from pytest_obligation.contract import Adoption, NotApplicable, Profile
    from pytest_obligation.contract import ObligationContract, due_work_contract_suite
    from pytest_obligation.crash_histories import HandoffHistory
    from pytest_obligation.host import Host
    from pytest_obligation.references import in_memory_handoffs as ref

    configure(Host(worker_killer=ref.ledger_killer))
    WHY = "the successor is committed separately"
    NA = NotApplicable("self-test")

    CONTRACT = ObligationContract(
        name="split handoff",
        adoption=Adoption.LEGACY,
        profiles={{profile: NA for profile in Profile}},
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


def run_child_session(directory: Path, pinned: str, *options: str) -> subprocess.CompletedProcess[str]:
    """
    Run :data:`SUITE`, pinning ``pinned``, in a pytest session rooted in ``directory``.

    The session gets its own ``pytest.ini``. Without one, pytest looks upward for
    a configuration file, and any it finds (a stray ``pyproject.toml`` in the
    shared temporary directory, for one) makes that ancestor the rootdir: the
    child reads its options, loads its conftest files, and scans the ancestor
    during collection, where another process deleting its own temporary entry
    fails the child with ``FileNotFoundError``.
    """
    (directory / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (directory / "test_split.py").write_text(dedent(SUITE.format(pinned=pinned)), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "handoff", str(directory), *options],
        cwd=directory,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )
