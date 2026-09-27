"""Every generated case carries the ``due_work`` mark, so CI can select exactly the harness's suites."""

from due_work_harness import due_work_contract_suite, safety_contract_suite

from .declarations import REFERENCE_CONTRACT


def _marks(cls: type, name: str) -> set[str]:
    return {mark.name for mark in getattr(cls, name).pytestmark}


def test_contract_and_safety_suites_are_marked_due_work() -> None:
    @due_work_contract_suite(REFERENCE_CONTRACT)
    class TestReference:
        pass

    assert REFERENCE_CONTRACT.safety is not None

    @safety_contract_suite(REFERENCE_CONTRACT.safety)
    class TestReferenceSafety:
        pass

    assert "due_work" in _marks(TestReference, "test_due_work_contract")
    assert "due_work" in _marks(TestReferenceSafety, "test_safety_contract")
