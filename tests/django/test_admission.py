"""Profile I through Django's real transaction ownership and the SQL fault adapter."""

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from functools import partial

import pytest

from pytest_obligation import AdmissionAtomicity
from pytest_obligation.host import hosted
from pytest_obligation.integrations.django import django_host
from pytest_obligation.integrations.django import lifecycle_references as ref
from pytest_obligation.integrations.django.admission import AdmissionInterrupted, interrupt_after_statement
from pytest_obligation.profiles.indivisible_admission import ADMISSION_PROOFS, assert_interrupted_admission_rolls_back

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def table() -> Iterator[None]:
    with ref.lifecycle_attempt_table(), hosted(django_host(production_packages={"pytest_obligation"})):
        yield


def admission(command: Callable[[int], None]) -> AdmissionAtomicity[str]:
    predecessor = ref.LifecycleAttempt.objects.create(status=ref.Status.RUNNING)

    def obligations() -> list[int]:
        return list(ref.LifecycleAttempt.objects.filter(retry_of=predecessor).values_list("pk", flat=True))

    def admit() -> list[int]:
        command(predecessor.pk)
        return obligations()

    return AdmissionAtomicity(
        name="Django retry admission",
        admit=admit,
        observe=lambda: ref.LifecycleAttempt.objects.get(pk=predecessor.pk).status,
        expected=ref.Status.RETRYABLE_FAILED,
        obligations=obligations,
        outstanding=obligations,
        effects=lambda: 0,
        publications=lambda: nullcontext(()),
        during=partial(
            interrupt_after_statement, lambda sql: sql.startswith('INSERT INTO "due_work_harness_lifecycleattempt"')
        ),
        expected_error=AdmissionInterrupted,
    )


@pytest.mark.parametrize("command", [ref.fail_attempt_with_atomic_handoff, ref.fail_attempt_with_savepoint_handoff])
@pytest.mark.parametrize("proof", ADMISSION_PROOFS, ids=lambda proof: proof.__name__)
def test_atomic_and_nested_admission_pass(
    command: Callable[[int], None], proof: Callable[[AdmissionAtomicity], None]
) -> None:
    proof(admission(command))


@pytest.mark.parametrize("command", [ref.fail_attempt_with_split_handoff, ref.fail_attempt_in_autocommit])
def test_independent_commits_cannot_pass(command: Callable[[int], None]) -> None:
    with pytest.raises(AssertionError, match="leaked"):
        assert_interrupted_admission_rolls_back(admission(command))
