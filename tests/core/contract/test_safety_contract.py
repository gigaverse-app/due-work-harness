"""The replay/retry profiles have one complete, annotated declaration path."""

from typing import Any

import pytest

from due_work_harness.contract import (
    Adoption,
    Claim,
    Decline,
    DueWorkContractDesignError,
    KnownGap,
    NotApplicable,
    Profile,
    SafetyContract,
    safety_contract_cases,
)
from due_work_harness.host import Host
from due_work_harness.references.in_memory import (
    reference_bounded_retry_binding,
    reference_replay_safety_binding,
)

_WHY = "self-test reason"


def _dispositions(**overrides: Any) -> dict[Profile, Any]:
    complete: dict[Profile, Any] = {profile: NotApplicable(_WHY) for profile in (Profile.H, Profile.J)}
    for key, value in overrides.items():
        complete[Profile[{"REPLAY_SAFE_EXECUTION": "H", "BOUNDED_RETRY": "J"}.get(key, key)]] = value
    return complete


def _annotated_replay():
    # ARRANGE — the reference binding builds a fresh in-memory operation.
    # REAL PRODUCTION — this self-test delegates to the harness reference transition.
    # EXTERNAL SEAM — none; this is the harness's in-memory oracle.
    # OBSERVE — the reference binding reads the complete operation state.
    return reference_replay_safety_binding()


def _unannotated_replay():
    return reference_replay_safety_binding()


def _docstring_only_replay():
    """
    ARRANGE: detached prose.
    REAL PRODUCTION: detached prose.
    EXTERNAL SEAM: detached prose.
    OBSERVE: detached prose.
    """
    return reference_replay_safety_binding()


def test_autocommit_safety_contract_marks_execution_proofs_transactionally(marking_host: Host) -> None:
    contract = SafetyContract(
        name="autocommit safety",
        transactional=True,
        profiles=_dispositions(REPLAY_SAFE_EXECUTION=Claim()),
        replay=_annotated_replay,
    )
    cases = [case for case in safety_contract_cases(contract) if "assert_" in case.id]
    assert cases
    for case in cases:
        mark = next(mark for mark in case.marks if mark.name == "database")
        assert mark.kwargs["transaction"] is True


def test_a_safety_declaration_requires_every_profile_disposition() -> None:
    with pytest.raises(DueWorkContractDesignError, match="no disposition for profile"):
        SafetyContract(
            name="self-test safety",
            profiles={Profile.H: Claim()},
            replay=_annotated_replay,
        )


def test_a_claim_requires_its_binding() -> None:
    with pytest.raises(DueWorkContractDesignError, match="is claimed but has no binding"):
        SafetyContract(name="self-test safety", profiles=_dispositions(BOUNDED_RETRY=Claim()))


def test_a_binding_without_a_claim_is_a_design_error() -> None:
    with pytest.raises(DueWorkContractDesignError, match="H is NotApplicable but `replay=` is bound"):
        SafetyContract(name="self-test safety", profiles=_dispositions(), replay=_annotated_replay)


def test_a_gap_on_a_binding_integrity_proof_is_refused() -> None:
    with pytest.raises(DueWorkContractDesignError, match="binding-integrity proof, which cannot be waived"):
        SafetyContract(
            name="self-test safety",
            adoption=Adoption.LEGACY,
            profiles=_dispositions(
                REPLAY_SAFE_EXECUTION=Claim(gaps={"assert_replay_transition_is_production_bound": _WHY})
            ),
            replay=_annotated_replay,
        )


def test_a_new_feature_safety_contract_refuses_gap_declarations() -> None:
    with pytest.raises(DueWorkContractDesignError, match="forbids gap declarations"):
        SafetyContract(name="self-test safety", profiles=_dispositions(BOUNDED_RETRY=KnownGap(_WHY)))


def test_a_domain_binding_requires_all_four_evidence_annotations() -> None:
    with pytest.raises(
        DueWorkContractDesignError,
        match="missing adopter evidence annotations.*ARRANGE.*REAL PRODUCTION.*EXTERNAL SEAM.*OBSERVE",
    ):
        SafetyContract(
            name="self-test safety",
            profiles=_dispositions(REPLAY_SAFE_EXECUTION=Claim()),
            replay=_unannotated_replay,
        )


def test_detached_docstring_annotations_do_not_satisfy_the_adopter_rule() -> None:
    with pytest.raises(DueWorkContractDesignError, match="missing adopter evidence annotations"):
        SafetyContract(
            name="self-test safety",
            profiles=_dispositions(REPLAY_SAFE_EXECUTION=Claim()),
            replay=_docstring_only_replay,
        )


def test_complete_safety_contract_generates_profile_rows() -> None:
    contract = SafetyContract(
        name="self-test safety",
        adoption=Adoption.LEGACY,
        profiles=_dispositions(REPLAY_SAFE_EXECUTION=Claim(), BOUNDED_RETRY=KnownGap(_WHY)),
        replay=_annotated_replay,
    )

    ids = {parameter.id for parameter in safety_contract_cases(contract)}

    assert ids == {
        "REPLAY_SAFE_EXECUTION-assert_replay_transition_is_production_bound",
        "REPLAY_SAFE_EXECUTION-assert_first_execution_has_visible_effect",
        "REPLAY_SAFE_EXECUTION-assert_replay_converges",
        "BOUNDED_RETRY-known_gap",
    }


def test_the_generated_replay_cases_pass_against_the_conforming_reference() -> None:
    contract = SafetyContract(
        name="self-test safety",
        profiles=_dispositions(REPLAY_SAFE_EXECUTION=Claim()),
        replay=_annotated_replay,
    )
    for parameter in safety_contract_cases(contract):
        parameter.values[0].run()


def test_safety_dispositions_distinguish_declined_from_not_applicable() -> None:
    contract = SafetyContract(
        name="self-test safety",
        profiles=_dispositions(
            REPLAY_SAFE_EXECUTION=Decline("provider send is intentionally not replay-safe"),
            BOUNDED_RETRY=NotApplicable("the domain performs no classified retries"),
        ),
    )

    ids = {parameter.id for parameter in safety_contract_cases(contract)}

    assert ids == {
        "REPLAY_SAFE_EXECUTION-declined",
        "BOUNDED_RETRY-not_applicable",
    }


def test_reference_retry_factory_remains_available_for_safety_claims() -> None:
    binding = reference_bounded_retry_binding()
    assert binding.max_executions > 0


def test_each_named_effect_gets_its_own_replay_proofs() -> None:
    """Image and document effects must both run; one passing effect cannot cover its sibling."""
    contract = SafetyContract(
        name="multiple effects",
        profiles=_dispositions(H=Claim()),
        replay={"image": _annotated_replay, "document": _annotated_replay},
    )
    cases = [row.values[0] for row in safety_contract_cases(contract) if row.values[0].profile is Profile.H]
    assert len(cases) == 6
    assert {case.id.split("-")[1] for case in cases} == {"image", "document"}
    for case in cases:
        case.run()
