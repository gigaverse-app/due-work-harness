"""The exemption decorator: it refuses an unproven claim, and its generated test runs the proof."""

from typing import Any

import pytest

from due_work_harness import DueWorkContractDesignError, DueWorkSource, exempt_due_work_suite
from due_work_harness.host import Host, hosted

REASON = "the next request re-derives the summary from the database"


def evict() -> None: ...


def _generated(cls: type) -> Any:
    """The test the decorator generated; added at runtime, so read from the class namespace."""
    return vars(cls)["test_the_exemption_holds"]


def test_an_exemption_runs_its_proof() -> None:
    ran: list[bool] = []

    @exempt_due_work_suite(DueWorkSource(evict), reason=REASON, prove=lambda: ran.append(True))
    class TestEviction:
        pass

    _generated(TestEviction)(TestEviction())
    assert ran == [True]
    assert [mark.name for mark in _generated(TestEviction).pytestmark] == ["due_work"]


def test_an_exemption_whose_proof_fails_names_its_reason() -> None:
    def refuted() -> None:
        raise AssertionError("the effect stayed absent")

    @exempt_due_work_suite(DueWorkSource(evict), reason=REASON, prove=refuted)
    class TestEviction:
        pass

    with pytest.raises(AssertionError, match="the effect stayed absent") as failure:
        _generated(TestEviction)(TestEviction())
    assert any(REASON in note for note in failure.value.__notes__)


def test_the_proof_runs_with_the_hosts_database_marks() -> None:
    marker = pytest.mark.database_for_exemption
    with hosted(Host(database_marks=lambda transactional: [marker(transaction=transactional)])):

        @exempt_due_work_suite(DueWorkSource(evict), reason=REASON, prove=lambda: None, transactional=True)
        class TestEviction:
            pass

    marks = {mark.name: mark.kwargs for mark in _generated(TestEviction).pytestmark}
    assert marks == {"due_work": {}, "database_for_exemption": {"transaction": True}}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reason": "fine", "prove": lambda: None}, "must name what absorbs the lost handoff"),
        ({"reason": REASON, "prove": None}, "must carry prove="),
    ],
    ids=["thin-reason", "no-proof"],
)
def test_an_unproven_or_thin_exemption_is_refused(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(DueWorkContractDesignError, match=message):
        exempt_due_work_suite(DueWorkSource(evict), **kwargs)  # pyrefly: ignore[bad-argument-type]


def test_an_exemption_takes_a_due_work_source() -> None:
    with pytest.raises(DueWorkContractDesignError, match="takes a DueWorkSource"):
        exempt_due_work_suite(evict, reason=REASON, prove=lambda: None)  # pyrefly: ignore[bad-argument-type]
