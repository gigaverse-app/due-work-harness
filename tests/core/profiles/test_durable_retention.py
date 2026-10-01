"""
The retention proofs (profile D), pointed at implementations we control.

Profile D had one invariant and no self-test module — an omission worth
noticing in itself, since a preservation-only contract is satisfiable by a
retention pass that deletes nothing (or a binding that never reaches the real
pass). The positive control closes that, and this module pins both directions
for both proofs, plus the binding guard.
"""

import pytest

from pytest_obligation.profiles.durable_retention import (
    assert_retention_contract,
    assert_retention_preserves_non_terminal_work,
    assert_retention_prunes_settled_work,
    assert_retention_transition_is_production_bound,
)
from pytest_obligation.references.in_memory import (
    InMemoryRetention,
    reference_retention_binding,
)


def test_the_conforming_retention_passes_every_proof() -> None:
    assert_retention_contract(reference_retention_binding())


def test_a_retention_keyed_on_age_alone_fails_preservation() -> None:
    class _DeletesEverything(InMemoryRetention):
        def run_retention(self) -> None:
            self.rows = {}

    with pytest.raises(AssertionError, match="deleted a non-terminal row"):
        assert_retention_preserves_non_terminal_work(reference_retention_binding(_DeletesEverything()))


def test_a_retention_that_never_prunes_fails_the_positive_control() -> None:
    """
    The vacuous counterfeit: a pass that deletes nothing preserves everything.
    Before the control existed, this implementation satisfied the whole
    profile.
    """

    class _NeverPrunes(InMemoryRetention):
        def run_retention(self) -> None:
            return None

    binding = reference_retention_binding(_NeverPrunes())
    assert_retention_preserves_non_terminal_work(binding)  # green — which is the point
    with pytest.raises(AssertionError, match="left a row the domain's own\\s+policy calls prunable"):
        assert_retention_prunes_settled_work(binding)


def test_the_binding_guard_rejects_a_test_module_pass() -> None:
    class _TestModuleRetention(InMemoryRetention):
        def run_retention(self) -> None:
            self.rows = {row_id: state for row_id, state in self.rows.items() if state != "SETTLED"}

    with pytest.raises(AssertionError, match="run_retention.*references no production"):
        assert_retention_transition_is_production_bound(reference_retention_binding(_TestModuleRetention()))
