"""
Child-pytest specimens, explicitly invoked by ``test_contract.test_generated_reference_pytest_outcomes``.

This filename is intentionally outside pytest's automatic test-file patterns:
the expected failures are the child result being asserted, not known bugs in
the suite.
"""

import os

import pytest

from pytest_obligation.contract import (
    Adoption,
    ExtraProof,
    KnownGap,
    Profile,
    due_work_contract_suite,
)
from tests.core.contract import declarations

mode = os.environ["DUE_WORK_REFERENCE_OUTCOME"]
contract = declarations.REFERENCE_CONTRACT
if mode != "conforming":
    # Reuse the conforming declaration; only the two intentionally broken proofs differ.
    contract = contract.model_copy(
        update={
            "adoption": Adoption.LEGACY,
            "profiles": {
                **contract.profiles,
                Profile.A: KnownGap(
                    "self-test: deliberately missing capability", detect=declarations.annotated_missing_extra
                ),
            },
            "extras": contract.extras
            + (
                ExtraProof(
                    name="known-broken-extra",
                    run=declarations.annotated_missing_extra,
                    gap="self-test: deliberately missing extra capability",
                    no_production_callable_because=declarations.SELF_TEST_NO_PRODUCTION,
                ),
            ),
        }
    )


@pytest.fixture(autouse=True)
def repair_reference_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    if mode == "repaired":
        # Repair only the stand-in proof; the real generator and pytest marks stay untouched.
        monkeypatch.setattr(declarations, "assert_the_reference_capability_exists", lambda: None)


@due_work_contract_suite(contract)
class TestInMemoryReferenceContract:
    """Run the actual generated methods so the parent can assert pytest's outcomes."""
