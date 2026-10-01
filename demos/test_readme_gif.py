"""
The README's demo GIF shows only what the harness prints.

``docs/demo.tape`` records ``docs/demo.gif`` against the demos in this
directory. This replays every command the tape types, in the same environment
(``docs/demo_env.sh``), and asserts the lines the GIF relies on: the diverging
history each finding names, the fix's proof passing, and the coverage scan's
count. A change in the harness or in a pinned upstream that moves one of those
lines fails here, and the GIF is re-recorded.

Needs what CI's demos job has: PostgreSQL through the PG* variables, and
``demos/fetch_upstream.py procrastinate dbos-demo-apps saleor`` run first.

    uv run --no-sync pytest demos/test_readme_gif.py -p no:django
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TAPE = ROOT / "docs" / "demo.tape"
ENV = ROOT / "docs" / "demo_env.sh"

#: Each command the GIF types, and lines its real output must contain.
SHOWN = {
    "pytest demos/procrastinate_demo_django -k 'Shipped and handoff' --xfail-tb -q": [
        "XFAILURES",
        "pytest_obligation.HistoriesDiverged: procrastinate demo_django: handoff 'create book': normal operation "
        "reaches [True], "
        "but these histories reach something else: {'worker died after commit 1': [False]}. Work was lost or repeated.",
        "1 xfailed",
    ],
    "pytest demos/procrastinate_demo_django -k 'Fixes and handoff' -v": [
        "TestTheDemoWithItsFixes::test_due_work_contract[handoff-create book-assert_crash_at_every_commit_converges] "
        "PASSED",
        "TestTheDemoWithItsFixes::test_due_work_contract[handoff-index_book admits set_indexed-"
        "assert_crash_at_every_commit_converges] PASSED",
        "2 passed",
    ],
    "pytest demos/dbos_transactional_outbox -p no:django -k C-known_gap --xfail-tb -q": [
        "XFAILURES",
        "pytest_obligation.HistoriesDiverged: place order: normal operation reaches ('SENT', 1), but these histories "
        "reach something else: "
        "{'died at after_send': ('SENT', 2)}. Work was lost or repeated.",
        "1 xfailed",
    ],
    "due-work-harness check --root demos/saleor_checkout": [
        "due-work-harness: every one of 137 handoff site(s) in 124 function(s) is accounted for [django, celery]",
    ],
}


def shown_commands() -> list[str]:
    """The commands the tape types while recording, in order: not its hidden setup, and not its comments."""
    commands: list[str] = []
    hidden = False
    for line in TAPE.read_text(encoding="utf-8").splitlines():
        if line in ("Hide", "Show"):
            hidden = line == "Hide"
            continue
        typed = re.fullmatch(r'Type(?:@\S+)? "(.*)"', line)
        if typed and not hidden and not typed.group(1).startswith("#"):
            commands.append(typed.group(1))
    return commands


def test_the_tape_types_exactly_the_commands_checked_here() -> None:
    assert shown_commands() == list(SHOWN)


@pytest.mark.parametrize("command", list(SHOWN))
def test_each_command_prints_what_the_gif_shows(command: str) -> None:
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    ran = subprocess.run(
        ["bash", "-c", f"source {ENV} && {command}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = ran.stdout + ran.stderr
    # A strict xfail and a passing proof both exit 0; so does a check that finds every site accounted for.
    assert ran.returncode == 0, output
    for line in SHOWN[command]:
        assert line in output, f"{command!r} no longer prints {line!r}:\n{output}"
