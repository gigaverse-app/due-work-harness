"""
The generated contract suites through the Django host, against PostgreSQL.

The framework-free self-tests in ``tests/core/contract`` pin the declaration
layer with a stand-in mark. These pin the two things only a real host can
show: the light contract's selection proofs pass end to end on a real
QuerySet, and the Django host hands generated cases pytest-django's
``django_db`` mark with real commits exactly where a proof needs them.
"""

from typing import Any

import pytest

from pytest_obligation.contract import (
    Adoption,
    Claim,
    Decline,
    KnownGap,
    NotApplicable,
    ObligationContract,
    Profile,
    SafetyContract,
    ScheduledSelection,
    contract_cases,
    safety_contract_cases,
    scheduled_selection_cases,
    scheduled_selection_suite,
)
from pytest_obligation.integrations.django.references import reference_scheduled_selection_queryset
from pytest_obligation.references.in_memory import reference_derivation_binding

_WHY = "self-test reason"


def _annotated_never_built():
    # ARRANGE — no fixture is built; these tests inspect collection metadata.
    # REAL PRODUCTION — none; this factory must never execute.
    # EXTERNAL SEAM — none; this factory must never execute.
    # OBSERVE — collection metadata is inspected without entering the binding.
    pytest.fail("never built during collection")


_REFERENCE_SELECTION = ScheduledSelection(
    name="self-test reference selection",
    due_work=reference_scheduled_selection_queryset,
    unscheduled_because="self-test: the reference selection exists only to run the suite end to end",
)


@scheduled_selection_suite(_REFERENCE_SELECTION)
class TestScheduledSelectionSuiteRuns:
    """
    The light contract end to end: a root-owned primary-key selection passes
    authorship, the index verdict, and replica routing. The deliberately
    authored direction is a design error, not a strict xfail — a gap on the
    authorship proof is refused (see the framework-free contract tests).
    """


def _django_db(param: Any) -> Any:
    return next(mark for mark in param.marks if mark.name == "django_db")


def test_the_django_host_gives_selection_cases_the_database() -> None:
    for param in scheduled_selection_cases(_REFERENCE_SELECTION):
        if param.id.startswith("assert_selection") or param.id.startswith("assert_the_adapter"):
            assert _django_db(param).kwargs.get("transaction") is False, param.id


def test_the_django_host_gives_cross_connection_proofs_real_commits() -> None:
    contract = ObligationContract(
        name="self-test contract",
        profiles={
            **{profile: NotApplicable(_WHY) for profile in Profile},
            Profile.A: Claim(),
            Profile.B: Claim(),
            Profile.F: Claim(),
            **{profile: NotApplicable(_WHY) for profile in (Profile.H, Profile.J)},
        },
        sweep=_annotated_never_built,
        ownership=_annotated_never_built,
        derivation=reference_derivation_binding,
    )
    params = {param.id: param for param in contract_cases(contract)}
    for name in ("A-assert_in_flight_work_is_not_duplicated", "B-assert_claim_is_exclusive_across_connections"):
        assert _django_db(params[name]).kwargs.get("transaction") is True, name
    assert _django_db(params["B-assert_claim_is_exclusive"]).kwargs.get("transaction") is False


@pytest.mark.parametrize("transactional", [True, False], ids=["transactional", "rolled-back"])
def test_a_declines_proof_and_a_known_gaps_probe_run_with_the_contracts_database(transactional: bool) -> None:
    # A probe that needs real commits must not run inside a test transaction: its
    # arrangement would be invisible to the production path it drives, and a strict
    # xfail would pass for the wrong reason.
    safety = SafetyContract(
        name="self-test contract",
        adoption=Adoption.LEGACY,
        transactional=transactional,
        profiles={
            Profile.H: Decline(_WHY, prove=reference_derivation_binding),
            Profile.J: KnownGap(_WHY, detect=reference_derivation_binding),
        },
    )
    contract = ObligationContract(
        name="self-test contract",
        adoption=Adoption.LEGACY,
        transactional=transactional,
        profiles={
            **{profile: NotApplicable(_WHY) for profile in Profile},
            Profile.B: Decline(_WHY, prove=reference_derivation_binding),
            Profile.C: KnownGap(_WHY, detect=reference_derivation_binding),
            **safety.profiles,
        },
    )
    params = [*contract_cases(contract), *safety_contract_cases(safety)]
    probing = {param.id: param for param in params if param.id.endswith(("-declined", "-known_gap"))}
    assert set(probing) == {"B-declined", "C-known_gap", "REPLAY_SAFE_EXECUTION-declined", "BOUNDED_RETRY-known_gap"}
    for case_id, param in probing.items():
        assert _django_db(param).kwargs.get("transaction") is transactional, case_id


def test_a_host_for_seeded_migrations_restores_them_after_committing_cases() -> None:
    from pytest_obligation.integrations.django import django_host

    host = django_host(production_packages=set(), serialized_rollback=True)
    (committing,) = host.database_marks(True)
    (rolled_back,) = host.database_marks(False)
    assert committing.kwargs == {"transaction": True, "serialized_rollback": True}
    # A case that rolls back never flushes, so it has nothing to restore.
    assert rolled_back.kwargs == {"transaction": False}
    (default,) = django_host(production_packages=set()).database_marks(True)
    assert default.kwargs == {"transaction": True}


def test_a_hand_written_test_gets_the_hosts_database_marks() -> None:
    from pytest_obligation import configure, due_work_database
    from pytest_obligation.integrations.django import django_host

    configure(django_host(production_packages=set(), serialized_rollback=True))

    @due_work_database()
    def findings() -> None:
        pass

    (mark,) = findings.pytestmark  # type: ignore[attr-defined]
    assert (mark.name, mark.kwargs) == ("django_db", {"transaction": True, "serialized_rollback": True})
