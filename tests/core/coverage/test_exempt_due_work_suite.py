"""The exemption decorator: it refuses an unproven or self-proven claim, and its generated test runs the proof."""

from functools import partial
from typing import Any

import pytest

from due_work_harness import DueWorkContractDesignError, DueWorkSource, LossIsAbsorbedElsewhere, exempt_due_work_suite
from due_work_harness.host import Host, hosted
from tests_support.sample_production import cache

REASON = "the next request re-derives the summary from the database"
KEY = 7


def evict() -> None: ...


def _generated(cls: type) -> Any:
    """The test the decorator generated; added at runtime, so read from the class namespace."""
    return vars(cls)["test_the_exemption_holds"]


def _proof(entries: set[int], absorb: Any = cache.rederive) -> LossIsAbsorbedElsewhere:
    return LossIsAbsorbedElsewhere(
        strand=lambda: KEY, observe=partial(cache.read_only, entries), absorb=partial(absorb, entries)
    )


def test_an_exemption_runs_its_proof() -> None:
    entries: set[int] = set()

    @exempt_due_work_suite(DueWorkSource(evict), reason=REASON, prove=_proof(entries))
    class TestEviction:
        pass

    _generated(TestEviction)(TestEviction())
    assert entries == {KEY}
    assert [mark.name for mark in _generated(TestEviction).pytestmark] == ["due_work"]


def test_an_exemption_whose_proof_fails_names_its_reason() -> None:
    @exempt_due_work_suite(DueWorkSource(evict), reason=REASON, prove=_proof(set(), absorb=cache.read_only))
    class TestEviction:
        pass

    with pytest.raises(AssertionError, match="did not produce the effect") as failure:
        _generated(TestEviction)(TestEviction())
    assert any(REASON in note for note in failure.value.__notes__)


def test_the_proof_runs_with_the_hosts_database_marks() -> None:
    marker = pytest.mark.database_for_exemption
    with hosted(Host(database_marks=lambda transactional: [marker(transaction=transactional)])):

        @exempt_due_work_suite(DueWorkSource(evict), reason=REASON, prove=_proof(set()), transactional=True)
        class TestEviction:
            pass

    marks = {mark.name: mark.kwargs for mark in _generated(TestEviction).pytestmark}
    assert marks == {"due_work": {}, "database_for_exemption": {"transaction": True}}


def _absorbed_by_the_test() -> None:
    """A proof the test wrote for itself: it proves only that the test can pass."""


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reason": "fine", "prove": _proof(set())}, "must name what absorbs the lost handoff"),
        ({"reason": REASON, "prove": None}, "must carry prove="),
        ({"reason": REASON, "prove": _absorbed_by_the_test}, "prove= is test-authored"),
        ({"reason": REASON, "prove": lambda: None}, "prove= is test-authored"),
        ({"reason": REASON, "prove": partial(_absorbed_by_the_test)}, "prove= is test-authored"),
    ],
    ids=["thin-reason", "no-proof", "test-function-proof", "test-lambda-proof", "test-partial-proof"],
)
def test_an_unproven_thin_or_self_proven_exemption_is_refused(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(DueWorkContractDesignError, match=message):
        exempt_due_work_suite(DueWorkSource(evict), **kwargs)  # pyrefly: ignore[bad-argument-type]


def test_an_exemption_takes_a_due_work_source() -> None:
    with pytest.raises(DueWorkContractDesignError, match="takes a DueWorkSource"):
        exempt_due_work_suite(evict, reason=REASON, prove=_proof(set()))  # pyrefly: ignore[bad-argument-type]
