"""The generic replay-safety proof, exercised against conforming and broken effects."""

import pytest

from pytest_obligation.profiles.harmless_replay import (
    assert_first_execution_has_visible_effect,
    assert_replay_converges,
    assert_replay_safety_contract,
    assert_replay_transition_is_production_bound,
)
from pytest_obligation.references.in_memory import (
    InMemoryReplayEffect,
    reference_replay_safety_binding,
)


def test_the_conforming_effect_passes_every_replay_proof() -> None:
    assert_replay_safety_contract(reference_replay_safety_binding())


def test_visible_churn_on_the_second_execution_fails_replay_safety() -> None:
    class _ChurningEffect(InMemoryReplayEffect):
        def execute(self, operation_id: int) -> None:
            self.reach_effect(operation_id)
            self.outputs[operation_id] = f"result-{self.executions + 1}"
            self.executions += 1

    binding = reference_replay_safety_binding(_ChurningEffect())
    assert_first_execution_has_visible_effect(binding)
    with pytest.raises(AssertionError, match="replaying the identical operation changed"):
        assert_replay_converges(reference_replay_safety_binding(_ChurningEffect()))


def test_an_effect_that_never_runs_fails_the_positive_control() -> None:
    class _NeverApplies(InMemoryReplayEffect):
        def execute(self, operation_id: int) -> None:
            return None

    binding = reference_replay_safety_binding(_NeverApplies())
    with pytest.raises(AssertionError, match="reached the external effect boundary 0"):
        assert_first_execution_has_visible_effect(binding)
    # Convergence used to PASS here, and this self-test asserted that it did:
    # an operation that never runs converges perfectly. That vacuous pass is
    # the whole counterfeit, so the replay proof now refuses it too.
    with pytest.raises(AssertionError, match="reached the external effect boundary 0"):
        assert_replay_converges(reference_replay_safety_binding(_NeverApplies()))


def test_a_replay_the_selection_skips_is_not_replay_safety() -> None:
    """
    The shape a sweep-shaped `execute` produces.

    Binding a whole tick as the replayable operation makes the *second* call a
    no-op: the first settled the row, so the selection no longer returns it.
    The observation is then trivially equal and the claim passes while the
    external effect has been executed exactly once — nothing about repeating
    it has been established. A broker redelivery that arrives before that
    settlement commits gets no such protection.
    """

    class _SettledAfterFirstRun(InMemoryReplayEffect):
        def execute(self, operation_id: int) -> None:
            if operation_id in self.outputs:
                return
            self.reach_effect(operation_id)
            self.outputs[operation_id] = "result"

    assert_first_execution_has_visible_effect(reference_replay_safety_binding(_SettledAfterFirstRun()))
    with pytest.raises(AssertionError, match="reached the external effect boundary 1"):
        assert_replay_converges(reference_replay_safety_binding(_SettledAfterFirstRun()))


def test_an_execute_that_ignores_the_prepared_identity_is_refused() -> None:
    """
    The binding guard for the same counterfeit, without needing to run it.

    An `execute` whose body never reads its parameter is not replaying the
    operation `prepare` arranged; it is running whatever its own selection
    finds.
    """
    effect = InMemoryReplayEffect()

    def ignores_its_identity(operation_id: int) -> None:
        InMemoryReplayEffect.execute(effect, 999)

    with pytest.raises(AssertionError, match="never reads its 'operation_id' parameter"):
        assert_replay_transition_is_production_bound(
            reference_replay_safety_binding(effect, execute=ignores_its_identity)
        )


def test_the_binding_guard_rejects_a_test_module_effect() -> None:
    effect = InMemoryReplayEffect()

    def local_execute(operation_id: int) -> None:
        effect.outputs[operation_id] = "test-authored"

    with pytest.raises(AssertionError, match="execute.*references no production"):
        assert_replay_transition_is_production_bound(reference_replay_safety_binding(effect, execute=local_execute))


def test_the_binding_guard_rejects_a_test_authored_transition() -> None:
    effect = InMemoryReplayEffect()

    def copied_execute(operation_id: int) -> None:
        effect.outputs.update({operation_id: "test-authored"})

    with pytest.raises(AssertionError, match="execute.*authors production semantics"):
        assert_replay_transition_is_production_bound(reference_replay_safety_binding(effect, execute=copied_execute))
