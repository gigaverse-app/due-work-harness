"""
The convergent-write proofs (profile E), pointed at implementations we control.

Profile E is expressed over opaque state values precisely so a pure function can
adopt it, which also makes it the easiest profile to self-test: the conforming
implementation lives in ``harness.due_work.references`` (root-owned, so the binding guard
accepts it) and every broken variant here is a few lines of plain Python. Each
broken variant fails exactly the proof that hunts its defect.
"""

from collections.abc import Callable

import pytest

from due_work_harness.profiles.eventual_convergence import (
    CONVERGENT_WRITE_PROOFS,
    assert_convergence_bindings_are_production_bound,
    assert_convergent_write_contract,
    assert_current_snapshot_writes,
    assert_reapplication_is_a_no_op,
    assert_settled_state_is_not_downgraded,
    assert_superseded_snapshot_contract,
    assert_superseded_snapshot_does_not_write,
    assert_unsettled_state_converges,
)
from due_work_harness.references.in_memory import (
    REFERENCE_SETTLED as _SETTLED,
)
from due_work_harness.references.in_memory import (
    GuardedWorker,
    reference_apply_evidence,
    reference_snapshot_binding,
)
from due_work_harness.references.in_memory import (
    reference_convergence_binding as _binding,
)


def _verdicts(apply_evidence: Callable[[tuple], tuple]) -> set[str]:
    failed: set[str] = set()
    for proof in CONVERGENT_WRITE_PROOFS:
        try:
            proof(_binding(apply_evidence))
        except AssertionError:
            failed.add(proof.__name__)
    return failed


def test_the_conforming_state_function_passes_every_proof() -> None:
    assert_convergent_write_contract(_binding(reference_apply_evidence))


@pytest.mark.parametrize(
    ("apply_evidence", "expected_failures"),
    [
        pytest.param(
            # Later evidence overwrites a settled result. Overwriting on every
            # application is also non-idempotent by construction, so the
            # reapplication proof fails on the same defect.
            lambda state: _SETTLED if state[0] != "SETTLED" else ("SETTLED", "newer"),
            {
                assert_settled_state_is_not_downgraded.__name__,
                assert_reapplication_is_a_no_op.__name__,
            },
            id="downgrades-settled-state",
        ),
        pytest.param(
            # Each application stamps a fresh revision — visible churn.
            lambda state: ("SETTLED", ("evidence",) + (state[1] or ())),
            {
                assert_reapplication_is_a_no_op.__name__,
                # The settled fixture also moves under it, so the
                # no-downgrade proof fails on the same defect.
                assert_settled_state_is_not_downgraded.__name__,
            },
            id="reapplication-churns",
        ),
        pytest.param(
            # Ignores all evidence: safe against stale writes, never converges.
            lambda state: state,
            {assert_unsettled_state_converges.__name__},
            id="ignores-evidence",
        ),
    ],
)
def test_each_broken_state_function_fails_exactly_its_own_proofs(
    apply_evidence: Callable[[tuple], tuple], expected_failures: set[str]
) -> None:
    assert _verdicts(apply_evidence) == expected_failures


def test_the_binding_guard_rejects_a_test_module_state_function() -> None:
    """
    A perfectly convergent state function implemented in a test module proves
    that function convergent and says nothing about production's. The guard
    accepts the root-owned reference (the composite above runs it) and rejects
    the same shape authored here.
    """

    def local_apply(state: tuple) -> tuple:
        return state if state[0] == "SETTLED" else _SETTLED

    with pytest.raises(AssertionError, match="apply_evidence.*references no production"):
        assert_convergence_bindings_are_production_bound(_binding(local_apply))


# --- The worker half: superseded snapshots -----------------------------------------


class _UnguardedWorker(GuardedWorker):
    """Breaks the superseded proof: writes whatever its captured input says."""

    def run(self, snapshot_key: str) -> None:
        self.published = f"result-from-{snapshot_key}"


class _NeverWritesWorker(GuardedWorker):
    """
    Breaks the positive control: the write path is never reached.

    This is the vacuous counterfeit the control exists to catch — an earlier
    form of this profile accepted a single superseded-run binding that could
    be ``lambda: None``, and "nothing observable changed" then passed without
    any worker having run.
    """

    def run(self, snapshot_key: str) -> None:
        return None


def test_a_guarded_worker_passes_the_snapshot_contract() -> None:
    assert_superseded_snapshot_contract(reference_snapshot_binding())


def test_an_unguarded_worker_fails_the_superseded_snapshot_proof() -> None:
    # Fresh bindings per proof, exactly as the generated suite builds them: a
    # reused worker would have already published the captured key's result,
    # making the stale write invisible as a repeat of the same value.
    assert_current_snapshot_writes(reference_snapshot_binding(_UnguardedWorker()))
    with pytest.raises(AssertionError, match="superseded snapshot wrote back"):
        assert_superseded_snapshot_does_not_write(reference_snapshot_binding(_UnguardedWorker()))


def test_a_worker_that_never_writes_fails_the_positive_control() -> None:
    binding = reference_snapshot_binding(_NeverWritesWorker())
    with pytest.raises(AssertionError, match="passes vacuously"):
        assert_current_snapshot_writes(binding)
    # The superseded proof alone would have been green — which is the point.
    assert_superseded_snapshot_does_not_write(binding)


def test_an_already_settled_unsettled_fixture_is_refused() -> None:
    """
    The dual of `settled_state`'s control, which existed from the start.

    One mistake in this one arrange binding voids TWO proofs: applying evidence
    to already-settled state is a no-op by the no-downgrade invariant, so
    re-application passes for the wrong reason, and convergence asserts a state
    is settled that arrived settled.
    """
    binding = _binding().model_copy(update={"unsettled_state": lambda: _SETTLED})
    for proof in (assert_reapplication_is_a_no_op, assert_unsettled_state_converges):
        with pytest.raises(AssertionError, match="is_settled\\(\\) already accepts"):
            proof(binding)
