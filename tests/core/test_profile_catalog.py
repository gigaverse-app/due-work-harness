"""Canonical profile identity and declaration completeness are independent of execution engines."""

import pytest

from due_work_harness.profiles.catalog import ConvergenceFamily, Profile


def test_profile_names_use_one_vocabulary_for_values_and_titles() -> None:
    assert "".join(profile.name for profile in Profile) == "ABCDEFGHIJ"
    assert all(profile.title.startswith(profile.name) for profile in Profile)
    assert Profile.GATED_EXECUTION is Profile.G
    assert Profile.HARMLESS_REPLAY is Profile.H
    assert Profile.INDIVISIBLE_ADMISSION is Profile.I
    assert Profile.JOB_RETRY_LIMITS is Profile.J
    assert all(profile.value == profile.title for profile in Profile)
    assert Profile.E.value == "Eventual Convergence"
    assert Profile.H.case_prefix == "REPLAY_SAFE_EXECUTION"
    assert Profile.J.case_prefix == "BOUNDED_RETRY"


def test_convergence_keeps_distinct_binding_families() -> None:
    assert {family.value for family in ConvergenceFamily} == {
        "snapshot",
        "convergence",
        "in_flight",
        "evidence_confluence",
    }


def test_every_profile_requires_an_explicit_assessment() -> None:

    from due_work_harness.contract import DueWorkContractDesignError
    from tests.core.contract.declarations import REFERENCE_CONTRACT

    for profile in Profile:
        with pytest.raises(DueWorkContractDesignError, match=f"no disposition for profile\\(s\\) {profile.name}"):
            REFERENCE_CONTRACT.model_copy(
                update={"profiles": {p: d for p, d in REFERENCE_CONTRACT.profiles.items() if p is not profile}}
            )


def test_an_existing_E_proof_does_not_claim_competing_event_families() -> None:
    from due_work_harness.contract import Claim, NotAssessed, convergence_assessments
    from tests.core.contract.declarations import REFERENCE_CONTRACT

    families = convergence_assessments(REFERENCE_CONTRACT)
    assert isinstance(families[ConvergenceFamily.STALE_SNAPSHOTS], Claim)
    assert isinstance(families[ConvergenceFamily.IN_FLIGHT], NotAssessed)
    assert isinstance(families[ConvergenceFamily.EVIDENCE_CONFLUENCE], NotAssessed)


def test_family_decisions_cannot_hide_bound_proofs_or_invent_missing_coverage() -> None:

    from due_work_harness.contract import Claim, DueWorkContractDesignError, NotApplicable
    from tests.core.contract.declarations import REFERENCE_CONTRACT

    for family, decision in (
        (ConvergenceFamily.STALE_SNAPSHOTS, NotApplicable("A bound proof cannot be made inapplicable.")),
        (ConvergenceFamily.IN_FLIGHT, Claim()),
    ):
        with pytest.raises(DueWorkContractDesignError, match="assessment contradicts its binding"):
            REFERENCE_CONTRACT.model_copy(update={"convergence_families": {family: decision}})


@pytest.mark.parametrize("case_id", ["I-not_assessed", "E-in_flight-not_assessed"])
def test_missing_assessment_generates_explicit_expected_failure(case_id: str) -> None:
    from due_work_harness.contract import NotAssessed, contract_cases
    from tests.core.contract.declarations import REFERENCE_CONTRACT

    reference = REFERENCE_CONTRACT.model_copy(
        update={
            "profiles": {**REFERENCE_CONTRACT.profiles, Profile.I: NotAssessed(because="Admission needs assessment.")}
        }
    )
    assert isinstance(reference.profiles[Profile.I], NotAssessed)
    row = next(p for p in contract_cases(reference) if p.id == case_id)
    assert row.values[0].assessment_only
    mark = next(mark for mark in row.marks if mark.name == "xfail")
    assert mark.kwargs["strict"] is True
    assert "adopt" in mark.kwargs["reason"]
    assert "KnownGap" in mark.kwargs["reason"]
    assert "NotApplicable" in mark.kwargs["reason"]
    with pytest.raises(pytest.fail.Exception, match="Not assessed"):
        row.values[0].run()


def test_family_gap_cannot_waive_missing_proofs() -> None:

    from due_work_harness.contract import DueWorkContractDesignError, KnownGap
    from tests.core.contract.declarations import REFERENCE_CONTRACT

    with pytest.raises(DueWorkContractDesignError, match="E-family gaps belong"):
        REFERENCE_CONTRACT.model_copy(
            update={
                "convergence_families": {
                    ConvergenceFamily.IN_FLIGHT: KnownGap("Missing a binding is not a production defect.")
                }
            }
        )
