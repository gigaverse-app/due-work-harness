"""An adopter's declaration really collects, runs, reports and optionally explores generated histories."""

import importlib.util

import pytest

from due_work_harness.interleavings import InFlightConvergence
from due_work_harness.interleavings.testing.reference import reference

pytest_plugins = ["pytester"]

DECLARATION = """
from due_work_harness import Claim, DueWorkContract, NotApplicable, Profile, due_work_contract_suite
from due_work_harness.interleavings import InFlightConvergence
from due_work_harness.interleavings.testing.reference import reference

scenario = InFlightConvergence(
    name="generated", bind=reference, intents=("A", "B", "C"), seams=("write",), independent=True,
    no_transport_because="This reference tests provider completion without a broker.",
)
contract = DueWorkContract(
    name="generated", profiles={**{p: NotApplicable("In-memory runner control.") for p in Profile}, Profile.E: Claim()},
    in_flight={scenario.name: scenario},
)
@due_work_contract_suite(contract)
class TestGenerated:
    pass
"""


@pytest.mark.parametrize("mode", ["off", "smoke"])
def test_generated_histories_and_optional_search_are_real_due_work_cases(pytester: pytest.Pytester, mode: str) -> None:
    if mode == "smoke" and importlib.util.find_spec("hypothesis") is None:
        pytest.skip("optional exploration extra is not installed")
    pytester.makepyfile(DECLARATION)
    scenario = InFlightConvergence(
        name="generated",
        bind=reference,
        intents=("A", "B", "C"),
        seams=("write",),
        independent=True,
        no_transport_because="This reference tests provider completion without a broker.",
    )
    result = pytester.runpytest_subprocess("-q", "-m", "due_work", f"--due-work-explore={mode}")
    result.assert_outcomes(
        passed=9 + len(scenario.histories()) + (mode == "smoke"), xfailed=3, deselected=(mode == "off")
    )


def test_explicit_search_without_the_optional_dependency_is_actionable(pytester: pytest.Pytester) -> None:
    if importlib.util.find_spec("hypothesis") is not None:
        pytest.skip("minimal-installation control runs in the core CI job")
    pytester.makepyfile(DECLARATION)
    result = pytester.runpytest_subprocess("--due-work-explore=smoke")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*Install due-work-harness[[]exploration[]]*"])


def test_assessment_gate_refuses_unassessed_families_even_when_deselected(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(DECLARATION)
    result = pytester.runpytest_subprocess("-q", "--due-work-require-assessed", "-k", "not not_assessed")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*generated*E/snapshot*not assessed*"])


def test_assessment_gate_accepts_explicit_family_decisions(pytester: pytest.Pytester) -> None:
    source = DECLARATION.replace(
        "in_flight={scenario.name: scenario},",
        'in_flight={scenario.name: scenario}, convergence_families={family: NotApplicable("No separate API in this reference.") '
        "for family in ConvergenceFamily if family != ConvergenceFamily.IN_FLIGHT},",
    )
    pytester.makepyfile("from due_work_harness.profiles.catalog import ConvergenceFamily\n" + source)
    result = pytester.runpytest_subprocess("-q", "--due-work-require-assessed")
    assert result.ret == pytest.ExitCode.OK


def test_assessment_gate_also_covers_standalone_safety_suites(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("""
from due_work_harness import NotApplicable, NotAssessed, Profile, SafetyContract, safety_contract_suite
contract = SafetyContract(name="safety", profiles={
    Profile.H: NotAssessed(because="The task's external effect has not been evaluated."),
    Profile.J: NotApplicable("The task is never retried."),
})
@safety_contract_suite(contract)
class TestSafety:
    pass
""")
    result = pytester.runpytest_subprocess("-q", "--due-work-require-assessed")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*safety*H*not assessed*"])
