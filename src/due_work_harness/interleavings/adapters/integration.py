"""Adapters to the existing contract case/report owner, not a second registry."""

from collections.abc import Mapping
from functools import partial

import pytest
from _pytest.mark.structures import ParameterSet

from ..bindings import InFlightConvergence, Scenario
from ..model import History, InterleavingFailure, KnownInterleavingFailure


def run_case(scenario: Scenario, history: History) -> None:
    try:
        scenario.run(history)
    except InterleavingFailure as error:
        gap = scenario.gaps.get(history.id)
        # Only this exact history/invariant pair is expected debt. Binding errors
        # bypass this handler, and a different behavioral failure must stay red.
        if gap is not None and error.invariant == gap.invariant:
            known = KnownInterleavingFailure(error.invariant, str(error))
            for note in getattr(error, "__notes__", ()):
                known.add_note(note)
            raise known from error
        raise


def validate(scenarios: Mapping[str, Scenario], *, legacy: bool) -> None:
    for name, scenario in scenarios.items():
        assert name == scenario.name, "scenario map key must match its stable name"
        scenario.validate_definition()
        assert legacy or not scenario.gaps, "new-feature interleavings cannot declare known gaps"
        assert type(scenario).__module__ == "due_work_harness.interleavings.bindings", (
            "adopters bind supported declarations, not custom runners"
        )


def cases(scenarios: Mapping[str, Scenario], fixtures: tuple[str, ...]) -> list[ParameterSet]:
    from ...contract import ContractCase, Profile, _database_marks

    result = []
    for name, scenario in scenarios.items():
        for history in scenario.histories():
            case = ContractCase(
                id=f"interleaving-{name}-{history.id}",
                run=partial(run_case, scenario, history),
                fixtures=fixtures,
                profile=Profile.E,
                family="in_flight" if isinstance(scenario, InFlightConvergence) else "evidence_confluence",
            )
            marks = _database_marks(True)
            if history.id in scenario.gaps:
                marks.append(
                    pytest.mark.xfail(
                        strict=True, raises=KnownInterleavingFailure, reason=scenario.gaps[history.id].reason
                    )
                )
            result.append(pytest.param(case, id=case.id, marks=marks))
    return result


def report(scenarios: Mapping[str, Scenario]) -> list[str]:
    result = []
    for name, scenario in scenarios.items():
        result.append(
            f"  Interleaving [{name}]: {len(scenario.histories())} unique fixed histories; "
            f"{len(scenario.gaps)} declared legacy gaps"
        )
        result.extend(f"    Not applicable: {family}: {reason}" for family, reason in scenario.limitations().items())
    return result


def install_exploration(cls: type, scenarios: Mapping[str, Scenario], fixtures: tuple[str, ...]) -> None:
    if not scenarios:
        return

    @pytest.mark.due_work
    @pytest.mark.due_work_exploration
    @pytest.mark.parametrize("interleaving_name", tuple(scenarios))
    def test_due_work_exploration(self: object, interleaving_name: str, request: pytest.FixtureRequest) -> None:
        lane = request.config.getoption("--due-work-explore", default="off")
        assert lane != "off", "disabled exploration must be deselected during collection"
        for fixture in fixtures:
            request.getfixturevalue(fixture)
        try:
            from ..exploration.hypothesis import explore
        except ModuleNotFoundError as error:
            if error.name != "hypothesis":
                raise
            raise pytest.UsageError("Install due-work-harness[exploration]") from error
        examples, steps = (20, 15) if lane == "smoke" else (200, 50)
        explore(scenarios[interleaving_name], max_examples=examples, max_steps=steps)

    assert not hasattr(cls, "test_due_work_exploration"), "generated exploration would shadow an existing method"
    from ...contract import _database_marks

    for mark in _database_marks(True):
        test_due_work_exploration = mark(test_due_work_exploration)
    cls.test_due_work_exploration = test_due_work_exploration
