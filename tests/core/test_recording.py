"""The recorded findings are Python source: evaluating it must give back exactly what was recorded."""

from typing import Any

import pytest

from due_work_harness.crash_histories import Findings, HistoryRun, findings_from
from due_work_harness.recording import FindingsRecorder, findings_literal, report


def _evaluated(findings: Findings) -> tuple[object, dict[str, object]]:
    return eval(findings_literal(findings), {"Findings": lambda delivered, outcomes: (delivered, outcomes)})  # noqa: S307


LABELS = {
    "consecutive commits fold": {f"worker died after commit {k}": "nothing" for k in range(1, 9)},
    "two in a row stay single": {f"worker died after commit {k}": "nothing" for k in (1, 2)},
    "a gap splits the run": {f"the reply to commit {k} was lost": "lost" for k in (1, 2, 3, 5, 6, 7)},
    "labels without numbers": {"notifications lost": "lost", "signal receiver failed": "failed"},
    "quotes in a label": {"the task's on_success hook raised": ("FAILURE", 1)},
    "mixed outcomes": {"worker died after commit 1": "a", "worker died after commit 2": "b", "callback 1": "a"},
    "braces in a folded label": {f"{{queue}} died after commit {k} of {{2}}": "x" for k in (1, 2, 3)},
    "braces in a single label": {"payload {'a': 1} refused": "x"},
    "digits before the number": {f"step 2 of callback {k}": "x" for k in (1, 2, 3, 4)},
}


@pytest.mark.parametrize("outcomes", LABELS.values(), ids=LABELS.keys())
def test_the_literal_evaluates_to_what_was_recorded(outcomes: dict[str, object]) -> None:
    delivered, evaluated = _evaluated(Findings("ok", outcomes))
    assert delivered == "ok"
    assert evaluated == outcomes


def test_consecutive_labels_fold_into_a_comprehension() -> None:
    literal = findings_literal(Findings("ok", LABELS["consecutive commits fold"]))
    assert "for k in range(1, 9)" in literal
    assert literal.count("worker died after commit") == 1


def _run(label: str, after: Any) -> HistoryRun:
    return HistoryRun(label=label, before=None, midway=None, after=after)


def test_findings_are_what_normal_operation_and_each_divergent_history_reach() -> None:
    runs = [_run("normal operation", "sent"), _run("died 1", "sent"), _run("died 2", "lost"), _run("died 3", "twice")]

    assert findings_from(runs) == Findings("sent", {"died 2": "lost", "died 3": "twice"})


def test_a_history_recorded_twice_is_reported_once() -> None:
    recorder = FindingsRecorder()
    recorder.record("place order", Findings("sent", {"died 1": "lost"}))
    recorder.record("place order", Findings("sent", {"died 1": "lost", "died 2": "lost"}))

    assert list(recorder.histories) == ["place order"]
    assert report(recorder).count("# place order") == 1
    assert "died 2" in report(recorder)


def test_a_converging_history_is_reported_as_such() -> None:
    recorder = FindingsRecorder()
    recorder.record("send mail", Findings("sent"))

    assert report(recorder) == "# send mail: converges"
