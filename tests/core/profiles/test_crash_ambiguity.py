"""
The ambiguity proofs (profile C), pointed at implementations we control.

Same discipline as the other harness self-test modules: a minimal conforming
in-memory implementation passes all six proofs, and each deliberately broken
variant fails exactly the invariant it breaks. The in-memory rows stand in for
the adopter's ``AmbiguityAware`` binding — the seam under test is the proofs,
not any real application domain.

The profile's pivot — invariants 2 and 3 are unsatisfiable together unless the
attempt is committed before the provider call — is exercised directly: the
degenerate that marks everything ambiguous and the one that retries everything
each fail their half of the pair.
"""

from collections.abc import Callable
from uuid import UUID

import pytest

from pytest_obligation.profiles.crash_ambiguity import (
    AMBIGUITY_PROOFS,
    AmbiguityAware,
    assert_ambiguity_bindings_are_production_bound,
    assert_ambiguity_contract,
    assert_ambiguity_is_not_resolved_by_blind_retry,
    assert_attempt_is_recorded_before_the_provider_call,
    assert_dead_claim_with_open_attempt_is_ambiguous,
    assert_dead_claim_without_attempt_is_retryable,
    assert_late_evidence_resolves_ambiguity,
    assert_ordinary_due_work_is_production_bound,
    assert_terminal_is_monotonic,
)
from pytest_obligation.references.in_memory import (
    REFERENCE_TERMINAL_STATES as _TERMINAL,
)
from pytest_obligation.references.in_memory import (
    InMemoryMachine as _InMemoryMachine,
)

# The conforming machine lives in harness.due_work.references — root-owned, because the
# binding guard rejects a test-module implementation that reaches no
# production code (see test_the_binding_guard_rejects_a_test_module_machine).
# The broken variants stay here: they run against individual behavioral
# proofs, where no binding guard is involved.


def _binding(machine: _InMemoryMachine) -> AmbiguityAware:
    return AmbiguityAware(
        name="in-memory reference machine",
        claim=machine.claim,
        start_attempt=machine.start_attempt,
        open_attempt=machine.open_attempt,
        expire_lease=machine.expire_lease,
        resolve_stalled=machine.resolve_stalled,
        ambiguous_disposition="MARK_UNKNOWN",
        retryable_disposition="REARM_READY",
        state_of=machine.state_of,
        ambiguous_state="UNKNOWN",
        terminal_states=_TERMINAL,
        apply_late_evidence=machine.apply_late_evidence,
        due_work_ids=machine.due_work_ids,
    )


class _FakeManager(list[int]):
    def due_for_retry(self) -> "_FakeManager":
        return self

    def filter(self, *args, **kwargs) -> "_FakeManager":
        return self

    def values_list(self, *fields, **kwargs) -> "_FakeManager":
        return self


class _FakeModel:
    objects = _FakeManager()


def _copied_due_work_query() -> set[int]:
    return set(_FakeModel.objects.filter(state__in=("READY", "RETRYABLE")).values_list("id", flat=True))


def test_profile_c_accepts_a_forwarder_to_a_production_selection() -> None:
    machine = _InMemoryMachine()
    binding = _binding(machine).model_copy(
        update={"due_work_ids": lambda: _FakeModel.objects.due_for_retry().values_list("id", flat=True)}
    )
    assert_ordinary_due_work_is_production_bound(binding)


@pytest.mark.parametrize(
    "copied_selection",
    [
        lambda: set(_FakeModel.objects.filter(state="READY").values_list("id", flat=True)),
        _copied_due_work_query,
    ],
    ids=("inline-copy", "helper-indirection"),
)
def test_profile_c_rejects_a_test_authored_due_work_selection(copied_selection: Callable[[], object]) -> None:
    machine = _InMemoryMachine()
    binding = _binding(machine).model_copy(update={"due_work_ids": copied_selection})

    with pytest.raises(AssertionError, match=r"due_work_ids.*test code.*filter"):
        assert_ordinary_due_work_is_production_bound(binding)


class _RecordsTheAttemptAfterTheCall(_InMemoryMachine):
    """
    Breaks invariant 1: the attempt is only visible once the provider replied.

    A crash during the call is then indistinguishable from a crash before it —
    which silently turns every ambiguous death into a blind retry.
    """

    def start_attempt(self, row_id: int, token: UUID) -> object:
        return object()  # nothing recorded


class _MarksEverythingAmbiguous(_InMemoryMachine):
    """Breaks invariant 3: safe but useless — nothing is ever retried."""

    def resolve_stalled(self, row_id: int) -> str:
        self.rows[row_id].state = "UNKNOWN"
        return "MARK_UNKNOWN"


class _RetriesEverything(_InMemoryMachine):
    """Breaks invariant 2: a maybe-applied effect is re-run blind."""

    def resolve_stalled(self, row_id: int) -> str:
        self.rows[row_id].state = "READY"
        return "REARM_READY"


class _RedispatchesAmbiguousRows(_InMemoryMachine):
    """Breaks invariant 4: UNKNOWN rows show up as ordinary due work."""

    def due_work_ids(self) -> set[int]:
        return {row_id for row_id, row in self.rows.items() if row.state in ("READY", "UNKNOWN")}


class _NeverResolvesAmbiguity(_InMemoryMachine):
    """Breaks invariant 5: ambiguity is a permanent leak."""

    def apply_late_evidence(self, row_id: int) -> str:
        return self.rows[row_id].state


class _ReopensSettledWork(_InMemoryMachine):
    """Breaks invariant 6: re-applied evidence moves a terminal row."""

    def apply_late_evidence(self, row_id: int) -> str:
        row = self.rows[row_id]
        row.state = "SENT" if row.state != "SENT" else "READY"
        return row.state


def _verdicts(make_machine: Callable[[], _InMemoryMachine]) -> set[str]:
    failed: set[str] = set()
    for proof in AMBIGUITY_PROOFS:
        try:
            proof(_binding(make_machine()))
        except AssertionError:
            failed.add(proof.__name__)
    return failed


def test_the_conforming_machine_passes_every_proof() -> None:
    assert_ambiguity_contract(_binding(_InMemoryMachine()))


@pytest.mark.parametrize(
    ("make_machine", "expected_failures"),
    [
        pytest.param(
            _RecordsTheAttemptAfterTheCall,
            {
                assert_attempt_is_recorded_before_the_provider_call.__name__,
                # With no attempt ever visible, a dead mid-call claim resolves
                # as retryable — which is invariant 2's failure, observed from
                # the recovery side. The pair failing together is the point:
                # attempt-before-call is what makes them separable. Proofs 4
                # and 5 fail on their setup for the same reason — they need an
                # ambiguous row and this machine can never produce one.
                assert_dead_claim_with_open_attempt_is_ambiguous.__name__,
                assert_ambiguity_is_not_resolved_by_blind_retry.__name__,
                assert_late_evidence_resolves_ambiguity.__name__,
            },
            id="attempt-recorded-too-late",
        ),
        pytest.param(
            _MarksEverythingAmbiguous,
            {assert_dead_claim_without_attempt_is_retryable.__name__},
            id="everything-ambiguous",
        ),
        pytest.param(
            _RetriesEverything,
            {
                assert_dead_claim_with_open_attempt_is_ambiguous.__name__,
                # 4 and 5 need an ambiguous row to exist; a machine that
                # retries everything cannot supply one, so they fail on setup.
                assert_ambiguity_is_not_resolved_by_blind_retry.__name__,
                assert_late_evidence_resolves_ambiguity.__name__,
            },
            id="everything-retried",
        ),
        pytest.param(
            _RedispatchesAmbiguousRows,
            {assert_ambiguity_is_not_resolved_by_blind_retry.__name__},
            id="ambiguous-rows-redispatched",
        ),
        pytest.param(
            _NeverResolvesAmbiguity,
            {
                assert_late_evidence_resolves_ambiguity.__name__,
                # 6 needs evidence that settles; with none, its setup fails.
                assert_terminal_is_monotonic.__name__,
            },
            id="ambiguity-never-resolves",
        ),
        pytest.param(
            _ReopensSettledWork,
            {assert_terminal_is_monotonic.__name__},
            id="terminal-reopened",
        ),
    ],
)
def test_each_broken_machine_fails_exactly_its_own_proofs(
    make_machine: Callable[[], _InMemoryMachine], expected_failures: set[str]
) -> None:
    assert _verdicts(make_machine) == expected_failures


def test_the_binding_guard_rejects_a_test_module_machine() -> None:
    """
    Profile C's counterfeit is a test-module state machine like the reference:
    it passes every behavioral proof while the production reaper does none of
    it. The composite runs the guard against the root-owned reference (above);
    the same transition authored in a test module must be rejected.
    """

    class _TestModuleMachine(_InMemoryMachine):
        def resolve_stalled(self, row_id: int) -> str:
            row = self.rows[row_id]
            if row.open_attempt is not None:
                row.state = "UNKNOWN"
                return "MARK_UNKNOWN"
            row.state = "READY"
            return "REARM_READY"

    with pytest.raises(AssertionError, match="resolve_stalled.*references no production"):
        assert_ambiguity_bindings_are_production_bound(_binding(_TestModuleMachine()))


def test_refusing_reapplication_by_raising_is_a_legitimate_design() -> None:
    """Invariant 6 accepts an implementation that raises on a second application."""

    class _RaisesOnReapplication(_InMemoryMachine):
        def apply_late_evidence(self, row_id: int) -> str:
            row = self.rows[row_id]
            if row.state in _TERMINAL:
                raise RuntimeError("already settled")
            row.state = "SENT"
            return row.state

    assert_terminal_is_monotonic(_binding(_RaisesOnReapplication()))
