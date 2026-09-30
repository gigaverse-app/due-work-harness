"""Profile I: a standalone command owns atomic product-intent and work admission.

The same production command runs successfully and under an interruption after
real partial writes. Assertions run before fault-fixture teardown: a fixture's
cleanup must never manufacture the rollback being certified.
"""

from collections.abc import Callable, Hashable, Iterable, Sequence
from contextlib import AbstractContextManager
from typing import Generic, TypeVar

import pytest

from due_work_harness.binding import TRANSITION_AUTHORING_OPERATIONS, assert_binding_reaches_production
from due_work_harness.helpers import proof_context
from due_work_harness.host import current_host
from due_work_harness.models import HarnessModel

ObservationT = TypeVar("ObservationT")


class AdmissionPublication(HarnessModel):
    """An observed wakeup; capture transaction state when it is actually published."""

    in_transaction: bool
    """True if publication happened before the admission transaction committed."""


class AdmissionAtomicity(HarnessModel, Generic[ObservationT]):
    """One freshly arranged standalone command, fault boundary, and independent observations."""

    name: str
    admit: Callable[[], Iterable[Hashable]]
    """Run the production command; return the identities of newly admitted obligations."""
    observe: Callable[[], ObservationT]
    """Read product intent independently of the returned obligations."""
    expected: ObservationT
    """The successful product result, supplied independently before running the command."""
    obligations: Callable[[], Iterable[Hashable]]
    """All persisted obligation identities, including terminal rows, in the test's isolated scope."""
    outstanding: Callable[[], Iterable[Hashable]]
    """Persisted identities still owed; newly admitted obligations must appear here."""
    effects: Callable[[], int]
    """Monotonic count of provider calls; admission must perform no external effect."""
    publications: Callable[[], AbstractContextManager[Sequence[AdmissionPublication]]]
    """Record wakeups without delivery, including transaction state at each publication."""
    during: Callable[[Callable[[], None]], AbstractContextManager[None]]
    """Inject expected_error after partial writes, calling the supplied checkpoint immediately before raising.

    This context must not wrap the command in a transaction or perform rollback.
    Its teardown may remove fault instrumentation only.
    """
    expected_error: type[Exception]
    """The specific injected failure; unrelated command errors must remain failures."""


def assert_admission_is_production_bound(binding: AdmissionAtomicity) -> None:
    """A test-authored transaction or compensating cleanup cannot substitute for production admission."""
    assert_binding_reaches_production(
        adopter=binding.name,
        field="admit",
        binding=binding.admit,
        forbidden=TRANSITION_AUTHORING_OPERATIONS,
        production_shape="the standalone production command owning admission",
    )


def _assert_admission(binding: AdmissionAtomicity, *, interrupt: bool) -> None:
    in_transaction: Callable[[], bool] = current_host().require("in_transaction")
    assert not in_transaction(), "admission must start outside a caller/test transaction"
    before = binding.observe()
    existing = set(binding.obligations())
    effects = binding.effects()
    assert before != binding.expected, "positive admission control must change product intent"
    reached = False

    def checkpoint() -> None:
        nonlocal reached
        # Both halves must really have been written: failure before admission is vacuous.
        assert set(binding.obligations()) - existing, "fault did not reach partial obligation writes"
        assert binding.observe() != before, "fault did not reach partial product writes"
        reached = True

    with proof_context(binding.publications()) as publications:
        if interrupt:
            with proof_context(binding.during(checkpoint)):
                assert not in_transaction(), "fault fixture must not supply admission's transaction"
                with pytest.raises(binding.expected_error):
                    tuple(binding.admit())
                assert reached, "injected failure never reached the admission checkpoint"
                assert not in_transaction(), "admission left its transaction open before fault teardown"
                assert set(binding.obligations()) == existing, "interrupted admission leaked or deleted obligations"
                assert binding.observe() == before, "interrupted admission leaked product intent"
                assert not publications, "interrupted admission published a wakeup"
                assert binding.effects() == effects, "admission performed an external effect"
        else:
            admitted = tuple(binding.admit())
            assert admitted and len(set(admitted)) == len(admitted), "admission returned no unique new obligations"
            assert not set(admitted) & existing, "admission reused preexisting obligations"
            assert set(binding.obligations()) == existing | set(admitted), "admission returned an incomplete work set"
            assert set(admitted) <= set(binding.outstanding()), "admitted work is not outstanding"
            assert binding.observe() == binding.expected, "successful admission did not commit product intent"
            assert all(not event.in_transaction for event in publications), "wakeup published before commit"
            assert binding.effects() == effects, "admission performed an external effect"
        assert not in_transaction(), "admission left its transaction open"


def assert_successful_admission_commits(binding: AdmissionAtomicity) -> None:
    """Successful admission commits product intent and fresh recoverable obligations together."""
    _assert_admission(binding, interrupt=False)


def assert_interrupted_admission_rolls_back(binding: AdmissionAtomicity) -> None:
    """Partial admission rolls back before fault teardown, without publishing or calling a provider."""
    _assert_admission(binding, interrupt=True)


ADMISSION_PROOFS = (
    assert_admission_is_production_bound,
    assert_successful_admission_commits,
    assert_interrupted_admission_rolls_back,
)
