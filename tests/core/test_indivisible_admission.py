"""Actual SQL transactions distinguish command-owned rollback from a fixture's imitation."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from sample_production.admission import AdmissionFault, Command, InterruptedAdmission

from pytest_obligation.host import Host, hosted
from pytest_obligation.profiles.indivisible_admission import (
    ADMISSION_PROOFS,
    AdmissionAtomicity,
    AdmissionPublication,
    assert_interrupted_admission_rolls_back,
    assert_successful_admission_commits,
)


@contextmanager
def admission(
    fault: AdmissionFault = "none", *, fixture_transaction: bool = False
) -> Iterator[AdmissionAtomicity[int]]:
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(
        "CREATE TABLE product (requested INTEGER); INSERT INTO product VALUES (0); CREATE TABLE obligations (id INTEGER);"
    )
    command = Command(db=db, fault=fault)
    events: list[AdmissionPublication] = []

    @contextmanager
    def publications() -> Iterator[list[AdmissionPublication]]:
        command.publish = lambda: events.append(AdmissionPublication(in_transaction=db.in_transaction))
        yield events

    @contextmanager
    def during(checkpoint: Callable[[], None]) -> Iterator[None]:
        def interrupt() -> None:
            checkpoint()
            raise InterruptedAdmission()

        command.checkpoint = interrupt
        if fixture_transaction:
            db.execute("BEGIN")
        try:
            yield
        finally:
            command.checkpoint = None
            # A dishonest fixture can rollback everything on exit. The proof must
            # already have detected leaked rows before this cleanup runs.
            db.rollback()

    try:
        with hosted(
            Host(in_transaction=lambda: db.in_transaction, production_packages=frozenset({"sample_production"}))
        ):
            yield AdmissionAtomicity(
                name="SQL command",
                admit=command.admit,
                observe=lambda: db.execute("SELECT requested FROM product").fetchone()[0],
                expected=1,
                obligations=lambda: tuple(row[0] for row in db.execute("SELECT id FROM obligations")),
                outstanding=lambda: tuple(row[0] for row in db.execute("SELECT id FROM obligations")),
                effects=lambda: command.provider_calls,
                publications=publications,
                during=during,
                expected_error=InterruptedAdmission,
            )
    finally:
        db.close()


@pytest.mark.parametrize("proof", ADMISSION_PROOFS, ids=lambda proof: proof.__name__)
def test_command_owned_atomic_admission_passes(proof: Callable[[AdmissionAtomicity], None]) -> None:
    with admission() as binding:
        proof(binding)


@pytest.mark.parametrize(
    "fault, message",
    [
        ("no_transaction", "leaked or deleted obligations"),
        ("partial_commit", "leaked or deleted obligations"),
        ("left_open", "left its transaction open"),
        ("publish_early", "published a wakeup"),
        ("publish_on_failure", "published a wakeup"),
        ("external_effect", "external effect"),
        ("missing_work", "partial obligation writes"),
        ("no_checkpoint", "DID NOT RAISE"),
    ],
)
def test_interrupted_admission_rejects_partial_or_external_effects(fault: AdmissionFault, message: str) -> None:
    with admission(fault) as binding:
        with pytest.raises((AssertionError, pytest.fail.Exception), match=message):
            assert_interrupted_admission_rolls_back(binding)


@pytest.mark.parametrize(
    "fault, message",
    [
        ("publish_early", "before commit"),
        ("external_effect", "external effect"),
        ("missing_work", "incomplete work set"),
        ("wrong_product", "product intent"),
    ],
)
def test_successful_control_requires_complete_committed_admission(fault: AdmissionFault, message: str) -> None:
    with admission(fault) as binding, pytest.raises(AssertionError, match=message):
        assert_successful_admission_commits(binding)


def test_the_fault_fixture_cannot_own_the_transaction() -> None:
    with admission(fixture_transaction=True) as binding, pytest.raises(AssertionError, match="fault fixture"):
        assert_interrupted_admission_rolls_back(binding)


def test_unrelated_error_is_not_the_promised_interruption() -> None:
    with admission("unrelated_error") as binding, pytest.raises(ValueError, match="unexpected command failure"):
        assert_interrupted_admission_rolls_back(binding)
