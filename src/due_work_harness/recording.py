"""
Record what every history leaves, as the ``Findings`` literal an adopter would pin.

Writing a findings table by hand means running the histories, copying each
divergent label and outcome, and grouping the runs that are the same
(``worker died after commit 1`` to ``8`` all leave nothing). ``pytest
--due-work-record-findings`` does that copying: for every handoff or process
history it runs, the divergent runs are printed after the session as a
``Findings(...)`` ready to paste, with consecutive commit numbers folded into a
comprehension.

The output is what the code does *today*. Reading it is the review: an entry
that is a loss belongs in the table under a declared gap with its reason; one
that is harmless gets a comment; nothing is pinned unread. While recording,
a declared table is not checked, so an out-of-date one can be regenerated.
"""

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from due_work_harness.crash_histories import Findings


class FindingsRecorder:
    """Collects the findings of the histories a session ran; installed by the pytest plugin."""

    def __init__(self) -> None:
        #: By history name: a history that runs twice in one session (a re-run, a second suite) is listed once.
        self.histories: dict[str, Findings] = {}

    def record(self, name: str, findings: "Findings") -> None:
        self.histories[name] = findings


_recorder: FindingsRecorder | None = None


def current_recorder() -> FindingsRecorder | None:
    return _recorder


def start_recording() -> FindingsRecorder:
    global _recorder
    _recorder = FindingsRecorder()
    return _recorder


def stop_recording() -> None:
    global _recorder
    _recorder = None


_NUMBERED = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)(?P<suffix>\D*)$")


def _folded(labels: list[str]) -> Iterator[tuple[str, tuple[str, int, int] | None]]:
    """Group labels differing only by one number into runs of three or more; the rest stay single."""
    index = 0
    while index < len(labels):
        match = _NUMBERED.match(labels[index])
        if match is None:
            yield labels[index], None
            index += 1
            continue
        prefix, suffix = match["prefix"], match["suffix"]
        start = int(match["number"])
        end = start
        while index + (end - start) + 1 < len(labels):
            following = _NUMBERED.match(labels[index + (end - start) + 1])
            if not (
                following
                and following["prefix"] == prefix
                and following["suffix"] == suffix
                and int(following["number"]) == end + 1
            ):
                break
            end += 1
        if end - start >= 2:
            yield labels[index], (_template(prefix, suffix), start, end)
            index += end - start + 1
        else:
            yield labels[index], None
            index += 1


def _template(prefix: str, suffix: str) -> str:
    """The label with its number as ``{k}``; braces in the label itself are escaped for the f-string."""
    escaped = (part.replace("{", "{{").replace("}", "}}") for part in (prefix, suffix))
    return "{k}".join(escaped)


def findings_literal(findings: "Findings") -> str:
    """The findings as Python source for a ``Findings``, the labels that leave the same outcome grouped."""
    by_outcome: dict[str, list[str]] = {}
    for label, outcome in findings.outcomes.items():
        by_outcome.setdefault(repr(outcome), []).append(label)
    lines = ["Findings(", f"    {findings.delivered!r},", "    {"]
    for outcome, labels in by_outcome.items():
        lines.append(f"        # {len(labels)} run(s) reach this")
        for label, run in _folded(labels):
            if run is None:
                lines.append(f"        {label!r}: {outcome},")
            else:
                template, start, end = run
                lines.append(f"        **{{f{template!r}: {outcome} for k in range({start}, {end + 1})}},")
    lines += ["    },", ")"]
    return "\n".join(lines)


def report(recorder: FindingsRecorder) -> str:
    """Every recorded history: its findings, or that it converges."""
    return "\n\n".join(
        f"# {name}\n{findings_literal(findings)}" if findings.outcomes else f"# {name}: converges"
        for name, findings in recorder.histories.items()
    )
