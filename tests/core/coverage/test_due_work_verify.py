"""
``pytest --due-work-verify``: a declaration counts only if its suite ran.

The static scan counts a declaration that exists; these runs show the session
failing when that declaration's suite did not run — deselected, or skipped at
import — and passing when it did.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .builders import write_project

# Django is imported where it is used, so the child session runs where Django is not installed.
ORDERS = """
    SUMMARIES: set[int] = set()

    def rename(order_id):
        from django.db import transaction

        transaction.on_commit(lambda: SUMMARIES.discard(order_id))

    def summary(order_id):
        SUMMARIES.add(order_id)

    def has_summary(order_id):
        return order_id in SUMMARIES
"""

EXEMPTION = """
    {preamble}
    from pytest_obligation import DueWorkSource, LossIsAbsorbedElsewhere, exempt_due_work_suite
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

PYTEST_OPTIONS = '\n[tool.pytest.ini_options]\npythonpath = ["."]\n'


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONPATH": ""}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rN", "--rootdir", str(root), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _project(root: Path, preamble: str = "") -> None:
    write_project(
        root,
        {"shop/orders.py": ORDERS, "tests/test_rename_exemption.py": EXEMPTION.format(preamble=preamble)},
        extra=PYTEST_OPTIONS,
    )


def test_a_session_that_ran_every_declared_suite_passes(tmp_path: Path) -> None:
    _project(tmp_path)
    completed = _run(tmp_path, "--due-work-verify", "-m", "due_work")
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("preamble", "arguments"),
    [
        ("", ("-k", "unrelated")),
        ("import pytest\n    pytest.importorskip('a_module_this_environment_lacks')", ()),
    ],
    ids=["deselected", "skipped-at-import"],
)
def test_a_declared_suite_that_did_not_run_fails_the_session(
    tmp_path: Path, preamble: str, arguments: tuple[str, ...]
) -> None:
    _project(tmp_path, preamble)
    assert _run(tmp_path, *arguments).returncode in (0, 5)  # without the option, nothing notices
    completed = _run(tmp_path, "--due-work-verify", *arguments)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert (
        "shop.orders.rename is exempt by tests/test_rename_exemption.py::TestRenameExemption, which ran no case "
        "in this session"
    ) in completed.stdout


#: Two xdist workers: each runs part of the session, and only the controller sees all of it.
XDIST = ("-p", "xdist", "-n", "2")


def test_a_session_split_across_workers_that_ran_every_declared_suite_passes(tmp_path: Path) -> None:
    pytest.importorskip("xdist")
    _project(tmp_path)
    completed = _run(tmp_path, "--due-work-verify", *XDIST)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "declared suites that did not run" not in completed.stdout


#: Each xdist worker records whether it holds a verdict of its own.
WORKER_REPORT = """
from pathlib import Path

def pytest_sessionfinish(session):
    worker = getattr(session.config, "workerinput", None)
    if worker is not None:
        verdict = session.config.pluginmanager.has_plugin("due-work-verify")
        Path(worker["workerid"] + ".verdict").write_text(str(verdict))
"""


def test_only_the_controller_holds_the_verdict(tmp_path: Path) -> None:
    pytest.importorskip("xdist")
    _project(tmp_path)
    (tmp_path / "conftest.py").write_text(WORKER_REPORT, encoding="utf-8")
    completed = _run(tmp_path, "--due-work-verify", *XDIST)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    verdicts = {path.stem: path.read_text() for path in tmp_path.glob("*.verdict")}
    assert verdicts == {"gw0": "False", "gw1": "False"}, "each worker would rescan the project for a partial verdict"


def test_a_declared_suite_deselected_across_workers_fails_the_session(tmp_path: Path) -> None:
    pytest.importorskip("xdist")
    _project(tmp_path)
    completed = _run(tmp_path, "--due-work-verify", "-k", "unrelated", *XDIST)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "shop.orders.rename is exempt by tests/test_rename_exemption.py::TestRenameExemption" in completed.stdout


def test_verification_needs_the_coverage_configuration(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    completed = _run(tmp_path, "--due-work-verify")
    assert completed.returncode == 4
    assert "has no [tool.due-work-harness] table" in completed.stderr
