"""The recorded findings are Python source: evaluating it must give back exactly what was recorded."""

import pytest

from due_work_harness.recording import RecordedHistory, findings_literal


def _evaluated(history: RecordedHistory) -> tuple[object, dict[str, object]]:
    return eval(findings_literal(history), {"Findings": lambda delivered, outcomes: (delivered, outcomes)})  # noqa: S307


LABELS = {
    "consecutive commits fold": {f"worker died after commit {k}": "nothing" for k in range(1, 9)},
    "two in a row stay single": {f"worker died after commit {k}": "nothing" for k in (1, 2)},
    "a gap splits the run": {f"the reply to commit {k} was lost": "lost" for k in (1, 2, 3, 5, 6, 7)},
    "labels without numbers": {"notifications lost": "lost", "signal receiver failed": "failed"},
    "quotes in a label": {"the task's on_success hook raised": ("FAILURE", 1)},
    "mixed outcomes": {"worker died after commit 1": "a", "worker died after commit 2": "b", "callback 1": "a"},
}


@pytest.mark.parametrize("outcomes", LABELS.values(), ids=LABELS.keys())
def test_the_literal_evaluates_to_what_was_recorded(outcomes: dict[str, object]) -> None:
    history = RecordedHistory(name="h", delivered="ok", outcomes=outcomes)
    delivered, evaluated = _evaluated(history)
    assert delivered == "ok"
    assert evaluated == outcomes


def test_consecutive_labels_fold_into_a_comprehension() -> None:
    literal = findings_literal(RecordedHistory(name="h", delivered="ok", outcomes=LABELS["consecutive commits fold"]))
    assert "for k in range(1, 9)" in literal
    assert literal.count("worker died after commit") == 1
