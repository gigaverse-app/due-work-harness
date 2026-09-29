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
from typing import Any

from pydantic import BaseModel, ConfigDict


class RecordedHistory(BaseModel):
    """One history's runs, as ``Findings`` would pin them."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    delivered: Any
    #: Label to outcome for every run that reached something other than normal operation, in run order.
    outcomes: dict[str, Any]


class FindingsRecorder:
    """Collects the histories a session ran; installed by the pytest plugin when recording is asked for."""

    def __init__(self) -> None:
        self.histories: list[RecordedHistory] = []

    def record(self, name: str, runs: list[Any]) -> None:
        delivered = runs[0].after
        outcomes = {run.label: run.after for run in runs[1:] if run.after != delivered}
        self.histories.append(RecordedHistory(name=name, delivered=delivered, outcomes=outcomes))


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
            yield f"{prefix}{{k}}{suffix}", (prefix + "{k}" + suffix, start, end)
            index += end - start + 1
        else:
            yield labels[index], None
            index += 1


def findings_literal(history: RecordedHistory) -> str:
    """The recorded history as Python source for a ``Findings``, grouped by outcome."""
    by_outcome: dict[str, list[str]] = {}
    for label, outcome in history.outcomes.items():
        by_outcome.setdefault(repr(outcome), []).append(label)
    lines = ["Findings(", f"    {history.delivered!r},", "    {"]
    for outcome, labels in by_outcome.items():
        lines.append(f"        # {len(labels)} run(s) reach this")
        for pattern, run in _folded(labels):
            if run is None:
                lines.append(f"        {pattern!r}: {outcome},")
            else:
                template, start, end = run
                lines.append(f"        **{{f{template!r}: {outcome} for k in range({start}, {end + 1})}},")
    lines += ["    },", ")"]
    return "\n".join(lines)


def report(recorder: FindingsRecorder) -> str:
    """Every recorded history, with the reminder that recording is not reviewing."""
    blocks = [
        f"# {history.name}\n{findings_literal(history)}" if history.outcomes else f"# {history.name}: converges"
        for history in recorder.histories
    ]
    return "\n\n".join(blocks)
