"""
The state-derivation proofs (profile F), pointed at implementations we control.

Both implementation shapes the profile has to describe are exercised: one that
materialises work records from product state (``derive`` is a reconciler), and
one whose selection *is* the derivation (``derive`` is a no-op and
``materialises_records=False``). The conforming version of each passes all four
proofs; each broken variant fails exactly the invariant it breaks — including
the edge-triggered shape, whose failure on invariant 1 is the profile's whole
discrimination.
"""

from collections.abc import Callable

import pytest

from pytest_obligation.coherence import (
    assert_automatic_recovery_consumes_derived_obligations,
)
from pytest_obligation.profiles.fact_derived_obligations import (
    STATE_DERIVED_PROOFS,
    StateDerived,
    assert_derivation_bindings_are_production_bound,
    assert_derivation_is_idempotent,
    assert_derived_transitions_are_production_bound,
    assert_moved_desired_state_is_superseded,
    assert_outstanding_selection_is_production_bound,
    assert_settlement_is_production_bound,
    assert_state_derived_contract,
    assert_stopped_work_is_not_revived,
    assert_unrecorded_obligation_is_discovered,
)
from pytest_obligation.references.in_memory import (
    MaterialisingDeriver as _MaterialisingDeriver,
)
from pytest_obligation.references.in_memory import (
    SelectionIsTheDerivation as _SelectionIsTheDerivation,
)
from pytest_obligation.references.in_memory import (
    WorkRecord as _Record,
)
from pytest_obligation.references.in_memory import (
    materialising_derivation_binding as _binding,
)
from pytest_obligation.references.in_memory import (
    reference_derivation_binding,
)

# The conforming derivers live in harness.due_work.references — root-owned, because the
# binding guard rejects a test-module implementation that reaches no
# production code (pinned below). The broken variants stay here: they run
# against individual behavioral proofs, where no binding guard is involved.


class _FakeManager(list[int]):
    def settle(self, product_id: int) -> None:
        del product_id

    def filter(self, **kwargs) -> "_FakeManager":
        return self

    def update(self, **kwargs) -> None:
        return None

    def values_list(self, *fields, **kwargs) -> "_FakeManager":
        return self


class _FakeModel:
    objects = _FakeManager()


def test_profile_f_accepts_a_forwarder_to_a_production_settlement() -> None:
    binding = _binding(_MaterialisingDeriver()).model_copy(
        update={"settle": lambda product_id: _FakeModel.objects.settle(product_id)}
    )
    assert_settlement_is_production_bound(binding)


def test_profile_f_rejects_a_test_authored_settlement_transition() -> None:
    binding = _binding(_MaterialisingDeriver()).model_copy(
        update={"settle": lambda product_id: _FakeModel.objects.filter(pk=product_id).update(state="SETTLED")}
    )

    with pytest.raises(AssertionError, match=r"settle.*test code.*update"):
        assert_settlement_is_production_bound(binding)


def test_profile_f_rejects_a_test_authored_outstanding_selection() -> None:
    binding = _binding(_MaterialisingDeriver()).model_copy(
        update={"outstanding": lambda: _FakeModel.objects.filter(state="DUE").values_list("id", flat=True)}
    )

    with pytest.raises(AssertionError, match=r"outstanding.*test code.*filter"):
        assert_outstanding_selection_is_production_bound(binding)


def test_profile_f_rejects_a_test_authored_desired_state_transition() -> None:
    binding = _binding(_MaterialisingDeriver()).model_copy(
        update={"move_desired_state": lambda product_id: _FakeModel.objects.filter(pk=product_id).update(file="moved")}
    )

    with pytest.raises(AssertionError, match=r"move_desired_state.*test code.*update"):
        assert_derived_transitions_are_production_bound(binding)


class _EdgeTriggered(_MaterialisingDeriver):
    """
    Breaks invariant 1: obligations exist only when a writer records them.

    ``derive`` never looks at product state, so a product created by a writer
    that never heard of the effect strands silently. This is the shape the
    profile exists to catch, and the shape an honest adopter *declines* the
    profile over rather than approximates.
    """

    def derive(self) -> None:
        return None


class _ChurnsEveryPass(_MaterialisingDeriver):
    """Breaks invariant 2: every pass rewrites the record."""

    def derive(self) -> None:
        super().derive()
        for record in self.records.values():
            record.revision += 1


class _GapFiller(_MaterialisingDeriver):
    """Breaks invariant 3: a settled record hides a moved desired state."""

    def derive(self) -> None:
        for product_id, product in self.products.items():
            if product.stopped or product_id in self.records:
                continue
            self.records[product_id] = _Record(desired=product.desired)


class _ReopensWithTheStaleState(_MaterialisingDeriver):
    """Breaks invariant 3's second half: re-opens, but wants the old state."""

    def derive(self) -> None:
        for product_id, product in self.products.items():
            if product.stopped or product.applied == product.desired:
                continue
            record = self.records.get(product_id)
            if record is None:
                self.records[product_id] = _Record(desired=product.desired)
            elif record.settled:
                record.settled = False  # re-opened, desired NOT refreshed
                record.revision += 1


class _RevivesStoppedWork(_MaterialisingDeriver):
    """
    Breaks invariant 4: cancellation loses to the next derivation pass.

    Identical to the conforming deriver except the ``stopped`` predicate is
    missing — the single omission the invariant exists to catch.
    """

    def derive(self) -> None:
        for product_id, product in self.products.items():
            if product.applied == product.desired:
                continue
            record = self.records.get(product_id)
            if record is None:
                self.records[product_id] = _Record(desired=product.desired)
            elif record.settled or record.desired != product.desired:
                record.desired = product.desired
                record.settled = False
                record.revision += 1


def _verdicts(make_deriver: Callable[[], _MaterialisingDeriver]) -> set[str]:
    failed: set[str] = set()
    for proof in STATE_DERIVED_PROOFS:
        try:
            proof(_binding(make_deriver()))
        except AssertionError:
            failed.add(proof.__name__)
    return failed


def test_the_conforming_deriver_passes_every_proof() -> None:
    assert_state_derived_contract(_binding(_MaterialisingDeriver()))


@pytest.mark.parametrize(
    ("make_deriver", "expected_failures"),
    [
        pytest.param(
            _EdgeTriggered,
            {
                assert_unrecorded_obligation_is_discovered.__name__,
                # 2 and 3 need a discovered obligation to observe; an
                # edge-triggered deriver cannot supply one, so their setup
                # assertions fail for the same underlying reason.
                assert_derivation_is_idempotent.__name__,
                assert_moved_desired_state_is_superseded.__name__,
                # 4 asserts an absence, and its positive control requires this
                # same pass to derive an ordinary obligation. An edge-triggered
                # deriver produces none, so the absence proves no stop
                # predicate — the control is doing its job here.
                assert_stopped_work_is_not_revived.__name__,
            },
            id="edge-triggered",
        ),
        pytest.param(
            _ChurnsEveryPass,
            {assert_derivation_is_idempotent.__name__},
            id="churns-every-pass",
        ),
        pytest.param(
            _GapFiller,
            {assert_moved_desired_state_is_superseded.__name__},
            id="gap-filler",
        ),
        pytest.param(
            _ReopensWithTheStaleState,
            {assert_moved_desired_state_is_superseded.__name__},
            id="reopens-with-stale-desired-state",
        ),
        pytest.param(
            _RevivesStoppedWork,
            {assert_stopped_work_is_not_revived.__name__},
            id="revives-stopped-work",
        ),
    ],
)
def test_each_broken_deriver_fails_exactly_its_own_proofs(
    make_deriver: Callable[[], _MaterialisingDeriver], expected_failures: set[str]
) -> None:
    assert _verdicts(make_deriver) == expected_failures


class _IdentityRotatingDerivation(_SelectionIsTheDerivation):
    """A product move may replace the durable row while preserving the obligation."""

    def __init__(self) -> None:
        super().__init__()
        self.replacements: dict[int, int] = {}

    def move_desired_state(self, product_id: int) -> str:
        moved_id = self.make_implied_obligation()
        self.products[product_id].stopped = True
        self.replacements[product_id] = moved_id
        return self.products[moved_id].desired

    def identity_after_move(self, product_id: int) -> int:
        return self.replacements[product_id]


def test_a_real_product_transition_may_rotate_the_obligation_identity() -> None:
    """An immutable-row lifecycle (replace, do not edit) satisfies invariant 3."""
    impl = _IdentityRotatingDerivation()
    binding = reference_derivation_binding(impl).model_copy(
        update={"move_desired_state": impl.move_desired_state, "identity_after_move": impl.identity_after_move}
    )
    assert_moved_desired_state_is_superseded(binding)


def test_a_selection_that_is_the_derivation_passes_every_proof() -> None:
    """``materialises_records=False`` is the declared no-op-derive shape."""
    assert_state_derived_contract(reference_derivation_binding(_SelectionIsTheDerivation()))


def test_the_materialises_records_guard_bites_a_recording_adapter() -> None:
    """
    An adapter whose ``make_implied_obligation`` records the obligation is
    exactly what invariant 1's pre-derivation guard exists to reject."""

    class _RecordsInsteadOfImplying(_MaterialisingDeriver):
        def make_implied_obligation(self) -> int:
            product_id = super().make_implied_obligation()
            self.records[product_id] = _Record(desired=self.products[product_id].desired)
            return product_id

    with pytest.raises(AssertionError, match="must create product\\s+state only"):
        assert_unrecorded_obligation_is_discovered(_binding(_RecordsInsteadOfImplying()))


# --- The binding guard, both directions ------------------------------------------


def test_the_binding_guard_rejects_a_test_module_deriver() -> None:
    """A reconciler re-implemented in a test module proves only itself."""

    class _TestModuleDeriver(_MaterialisingDeriver):
        def derive(self) -> None:
            for product_id, product in self.products.items():
                if product.stopped or product.applied == product.desired:
                    continue
                if product_id not in self.records:
                    self.records[product_id] = _Record(desired=product.desired)

    with pytest.raises(AssertionError, match="derive.*references no production"):
        assert_derivation_bindings_are_production_bound(_binding(_TestModuleDeriver()))


# --- A↔F coherence -----------------------------------------------------------------


class _AgedSweep:
    """
    A stand-in profile A surface over the reference derivation's products.

    ``make_recovery_eligible`` marks a product aged into the recovery window;
    ``due_work`` selects aged, unstopped products by the ``narrow`` predicate
    under test; the tick dispatches what it selects, recorded. The conforming
    predicate matches the derivation (outstanding = desired != applied); the
    narrow one adds ``applied is None`` — never settled — which is exactly the
    live finding this proof came from: a sweep serving only the never-settled
    rows while the derivation also re-opens settled-then-moved ones.
    """

    def __init__(self, impl: _SelectionIsTheDerivation, *, narrow: bool) -> None:
        self.name = "in-memory aged sweep"
        self.impl = impl
        self.narrow = narrow
        self.aged: set[int] = set()
        self.last_dispatched: list[int] = []

    def make_recovery_eligible(self, product_id: int) -> None:
        self.aged.add(product_id)

    def due_work(self) -> list[int]:
        selected = []
        for product_id in self.impl.outstanding():
            if product_id not in self.aged:
                continue
            if self.narrow and self.impl.products[product_id].applied is not None:
                continue
            selected.append(product_id)
        return selected

    def identity_of(self, row: int) -> int:
        return row

    def run_tick(self) -> int:
        self.last_dispatched = list(self.due_work())
        return len(self.last_dispatched)

    def dispatched_ids(self) -> list[int]:
        return list(self.last_dispatched)


def _coherence_pair(*, narrow: bool) -> tuple[_AgedSweep, StateDerived]:
    impl = _SelectionIsTheDerivation()
    sweep = _AgedSweep(impl, narrow=narrow)
    derived = reference_derivation_binding(impl).model_copy(
        update={"make_recovery_eligible": sweep.make_recovery_eligible}
    )
    return sweep, derived


def test_a_recovery_that_consumes_the_derivation_passes_coherence() -> None:
    sweep, derived = _coherence_pair(narrow=False)
    assert_automatic_recovery_consumes_derived_obligations(sweep, derived)


def test_a_sweep_narrower_than_the_derivation_fails_coherence() -> None:
    """Both profiles green, settled-then-moved rows recoverable by nobody."""
    sweep, derived = _coherence_pair(narrow=True)
    with pytest.raises(AssertionError, match=r"recovery never\s+selects it"):
        assert_automatic_recovery_consumes_derived_obligations(sweep, derived)


def test_coherence_requires_the_eligibility_binding() -> None:
    sweep, _ = _coherence_pair(narrow=False)
    with pytest.raises(AssertionError, match="make_recovery_eligible"):
        assert_automatic_recovery_consumes_derived_obligations(
            sweep,
            reference_derivation_binding(_SelectionIsTheDerivation()),
        )


def test_an_inert_derivation_cannot_prove_stopped_work_is_not_revived() -> None:
    """
    Invariant 4 asserts an absence, and needed a control that anything derives.

    A derivation producing nothing at all — inert bindings, a fixture in a
    state nothing derives from, a `make_stopped` that built something the
    predicate was never going to consider — satisfies "the stopped obligation
    is absent" while proving no stop predicate exists.
    """
    binding = reference_derivation_binding().model_copy(update={"outstanding": lambda: []})
    with pytest.raises(AssertionError, match="derived no ordinary obligation at all"):
        assert_stopped_work_is_not_revived(binding)
