"""Opt-in schedule search. Imported only when exploration is explicitly requested."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypothesis import strategies as st

from ..bindings import EvidenceConfluence, InFlightConvergence, Scenario
from ..engine.catalog import step
from ..engine.causality import available_facts
from ..model import History, Step
from ..model import Operation as Op


def _draw_history(draw: "st.DrawFn", scenario: Scenario, max_steps: int) -> History:
    """Draw legal alias-only histories within the budget, preserving causal prerequisites."""
    from hypothesis import strategies as st

    if isinstance(scenario, InFlightConvergence) and not scenario.retirement and len(scenario.intents) >= 3:
        seam = draw(st.sampled_from(scenario.seams))
        current = scenario.intents[0]
        actions = [step(Op.ADMIT, value=current), step(Op.ARM, seam=seam), step(Op.START)]
        count = draw(st.integers(min_value=1, max_value=max(1, (max_steps - 7) // 4)))
        for _ in range(count):
            current = draw(st.sampled_from(tuple(value for value in scenario.intents if value != current)))
            actions += [step(Op.ARM, seam=seam), step(Op.CHANGE, value=current)]
        for index in draw(st.permutations(tuple(range(count + 1)))):
            actions += [step(Op.COMPLETE, index=index), step(Op.RECOVER)]
        actions += [step(Op.SETTLE), step(Op.QUIET)]
    elif isinstance(scenario, EvidenceConfluence):
        seen: set[str] = set()
        actions = [step(Op.PREPARE)]
        count = draw(st.integers(min_value=0, max_value=max(0, (max_steps - 8) // 2)))
        for _ in range(count):
            available = available_facts(scenario.facts, scenario.dependencies, seen)
            fact = draw(st.sampled_from((*available, "")))
            if fact and scenario.batchable:
                group = draw(st.lists(st.sampled_from(available), min_size=1, max_size=len(available), unique=True))
                actions += [Step(operation=Op.BATCH, facts=tuple(group)), step(Op.CHECK_EVIDENCE)]
                seen.update(group)
            elif fact:
                actions += [step(Op.EVIDENCE, value=fact), step(Op.CHECK_EVIDENCE)]
                seen.add(fact)
            else:
                actions += [step(Op.RECOVER), step(Op.CHECK_EVIDENCE)]
        while len(seen) < len(scenario.facts):
            available = tuple(f for f in available_facts(scenario.facts, scenario.dependencies, seen) if f not in seen)
            fact = draw(st.sampled_from(available))
            actions += [step(Op.EVIDENCE, value=fact), step(Op.CHECK_EVIDENCE)]
            seen.add(fact)
    else:
        # Retirements are irreversible: vary finite recovery gaps without inventing reactivation.
        eligible = tuple(h for h in scenario.histories() if len(h.steps) <= max_steps)
        assert eligible, "exploration step budget cannot fit any declared history"
        base = draw(st.sampled_from(eligible))
        actions = []
        budget = max(0, max_steps - len(base.steps))
        for item in base.steps:
            if item.operation == Op.COMPLETE and budget:
                repeats = draw(st.integers(min_value=0, max_value=budget))
                actions.extend(step(Op.RECOVER) for _ in range(repeats))
                budget -= repeats
            actions.append(item)
    return History(id="exploration", families=("exploration",), steps=tuple(actions))


def histories(scenario: Scenario, max_steps: int) -> "st.SearchStrategy[History]":
    """Build the optional strategy without importing Hypothesis on ordinary core imports."""
    from hypothesis import strategies as st

    return st.composite(_draw_history)(scenario, max_steps)


def explore(scenario: Scenario, *, max_examples: int, max_steps: int) -> None:
    """
    Search and shrink schedules using fresh bindings and the same invariant interpreter.

    max_examples must be positive and max_steps at least 11. Deterministic generation
    needs no persisted Hypothesis database; failures include a portable HistoryTrace.
    Legacy fixed-history xfails do not suppress exploration failures.
    """
    try:
        from hypothesis import given, note, settings
    except ModuleNotFoundError as error:
        if error.name != "hypothesis":
            raise
        raise RuntimeError("Install pytest-obligation[exploration] to search generated schedules") from error

    scenario.validate_definition()
    assert max_examples > 0 and max_steps >= 11, "exploration requires positive examples and at least 11 steps"

    @settings(max_examples=max_examples, deadline=None, database=None, derandomize=True)
    @given(histories(scenario, max_steps))
    def execute(history: History) -> None:
        note(history.model_dump_json())
        # run() enters a new environment for every generated/shrinking example.
        scenario.run(history)

    execute()
