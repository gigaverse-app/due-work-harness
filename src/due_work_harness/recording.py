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

from due_work_harness.models import HarnessModel

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


#: Labels are folded into a comprehension only from this many in a row; two are clearer written out.
MIN_FOLDED = 3

_NUMBERED = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)(?P<suffix>\D*)$")


class NumberedRun(HarnessModel):
    """Consecutive labels that differ only by one number: ``worker died after commit 1`` to ``8``."""

    #: The label with the number as ``{k}``, braces of the label itself escaped, ready for an f-string.
    template: str
    first: int
    last: int


def _split(label: str) -> tuple[str, int, str] | None:
    """The label as (text before its last number, the number, text after it), or None without a number."""
    match = _NUMBERED.match(label)
    return (match["prefix"], int(match["number"]), match["suffix"]) if match else None


def _run_length(labels: list[str], index: int, parts: tuple[str, int, str]) -> int:
    """How many labels from ``index`` are the same label with its number going up by one each time."""
    prefix, number, suffix = parts
    length = 1
    while index + length < len(labels) and _split(labels[index + length]) == (prefix, number + length, suffix):
        length += 1
    return length


def _template(prefix: str, suffix: str) -> str:
    escaped = (part.replace("{", "{{").replace("}", "}}") for part in (prefix, suffix))
    return "{k}".join(escaped)


def _folded(labels: list[str]) -> Iterator[str | NumberedRun]:
    """The labels in order, each run of :data:`MIN_FOLDED` or more numbered ones replaced by one ``NumberedRun``."""
    index = 0
    while index < len(labels):
        parts = _split(labels[index])
        length = _run_length(labels, index, parts) if parts else 1
        if parts and length >= MIN_FOLDED:
            prefix, number, suffix = parts
            yield NumberedRun(template=_template(prefix, suffix), first=number, last=number + length - 1)
        else:
            length = 1
            yield labels[index]
        index += length


def findings_literal(findings: "Findings") -> str:
    """The findings as Python source for a ``Findings``, the labels that leave the same outcome grouped."""
    by_outcome: dict[str, list[str]] = {}
    for label, outcome in findings.outcomes.items():
        by_outcome.setdefault(repr(outcome), []).append(label)
    lines = ["Findings(", f"    {findings.delivered!r},", "    {"]
    for outcome, labels in by_outcome.items():
        lines.append(f"        # {len(labels)} run(s) reach this")
        for item in _folded(labels):
            if isinstance(item, NumberedRun):
                comprehension = f"f{item.template!r}: {outcome} for k in range({item.first}, {item.last + 1})"
                lines.append(f"        **{{{comprehension}}},")
            else:
                lines.append(f"        {item!r}: {outcome},")
    lines += ["    },", ")"]
    return "\n".join(lines)


def report(recorder: FindingsRecorder) -> str:
    """Every recorded history: its findings, or that it converges."""
    return "\n\n".join(
        f"# {name}\n{findings_literal(findings)}" if findings.outcomes else f"# {name}: converges"
        for name, findings in recorder.histories.items()
    )
