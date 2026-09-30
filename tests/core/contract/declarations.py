"""
Declaration building blocks shared by the contract self-tests and the child-pytest specimens.

A plain module rather than a test module, so ``reference_contract_cases`` can
reuse the conforming declaration without importing a test file. It lives under
``tests/``, so every binding defined here is test code to the tripwires — the
same position an adopter's declarations are in.

The stand-in ``assert_*`` proofs these declarations delegate to live in
:mod:`due_work_harness.references.in_memory`: the bespoke-assertion check
resolves referenced callables and only a harness-defined proof counts, so a
test-module helper merely named ``assert_*`` is itself one of the refusals the
self-tests pin.
"""

from typing import Any

import pytest

from due_work_harness.contract import (
    Claim,
    Decline,
    DueWorkContract,
    ExtraProof,
    NotApplicable,
    Profile,
    SafetyContract,
)
from due_work_harness.references.in_memory import (
    assert_self_test_probe_fires,
    assert_the_reference_capability_exists,
)
from due_work_harness.references.in_memory import (
    reference_derivation_binding as derivation_binding,
)
from due_work_harness.references.in_memory import (
    reference_snapshot_binding as snapshot_binding,
)

#: Reasons reused across cases; the tests assert refusal, not prose.
WHY = "self-test reason"

#: The root self-test's stand-ins are in-memory by construction — that is what
#: makes them a reference — so they declare the escape real adopters must
#: justify rather than pretending to reach a production callable.
SELF_TEST_NO_PRODUCTION = (
    "the root self-test's stand-in proof is an in-memory reference by construction; "
    "it exists to exercise the generation layer, not a domain"
)


def annotated_never_built():
    # ARRANGE — no fixture is built; these tests inspect collection metadata.
    # REAL PRODUCTION — none; this factory must never execute.
    # EXTERNAL SEAM — none; this factory must never execute.
    # OBSERVE — collection metadata is inspected without entering the binding.
    pytest.fail("never built during collection")


def annotated_empty_selection():
    # ARRANGE — no rows are needed for declaration/case-generation self-tests.
    # REAL PRODUCTION — none; this is a root harness self-test.
    # EXTERNAL SEAM — none; this is a root harness self-test.
    # OBSERVE — tests inspect generated metadata or the expected failure shape.
    return None


def annotated_extra() -> None:
    # ARRANGE — the root self-test supplies an empty observation recorder.
    observations: list[bool] = []
    # EXTERNAL SEAM — none; this delegates to the shared harness assertion.
    # REAL PRODUCTION — the root harness assertion owns the proof semantics.
    assert_self_test_probe_fires(observations)
    # OBSERVE — the shared assertion verifies the recorder was mutated.


def annotated_missing_extra() -> None:
    # ARRANGE — the reference capability is deliberately absent.
    # EXTERNAL SEAM — none; this is the harness's strict-xfail reference.
    # REAL PRODUCTION — the root harness assertion owns the proof semantics.
    assert_the_reference_capability_exists()
    # OBSERVE — the assertion's failure is the expected known-gap evidence.


def dispositions(**overrides: Any) -> dict[Profile, Any]:
    """A complete, all-inapplicable disposition map, overridable per profile letter."""
    complete: dict[Profile, Any] = {profile: NotApplicable(WHY) for profile in Profile}
    for key, value in overrides.items():
        complete[Profile[key]] = value
    return complete


def safety_contract(name: str = "self-test contract") -> SafetyContract:
    return SafetyContract(name=name, profiles={profile: NotApplicable(WHY) for profile in (Profile.H, Profile.J)})


#: A conforming contract the child-pytest specimens run end to end: profiles E
#: and F claimed against the in-memory references, one passing extra.
REFERENCE_CONTRACT = DueWorkContract(
    name="in-memory reference",
    profiles={
        Profile.A: NotApplicable("self-test: the conforming in-memory reference has no sweep"),
        Profile.B: Decline("self-test: the in-memory reference has no ownership surface"),
        Profile.C: NotApplicable("self-test: no provider exists"),
        Profile.D: NotApplicable("self-test: nothing prunes"),
        Profile.E: Claim(),
        Profile.F: Claim(),
        Profile.H: NotApplicable("Self-test has no external operation to replay."),
        Profile.J: NotApplicable("Self-test has no retry execution budget."),
        Profile.G: NotApplicable("Self-test has no execution gate."),
        Profile.I: NotApplicable("Self-test has no admission command."),
    },
    snapshot=snapshot_binding,
    derivation=derivation_binding,
    extras=(
        ExtraProof(
            name="passing-extra",
            run=annotated_extra,
            no_production_callable_because=SELF_TEST_NO_PRODUCTION,
        ),
    ),
)
