"""
Invariants 2b/2c against the reference attempt lifecycle, conforming and broken.

The negative controls reproduce the defect that motivated these proofs: a
retrying pipeline commits ``RETRYABLE_FAILED`` in one transaction and the
worker creates the successor it owes later, while the selection never returns
``RETRYABLE_FAILED``. The conforming control commits both together. Each
defense also has a focused mutant: an undeclared state, a no-op execution, a
publication or commit-callback handoff, a provider call, and a counterfeit
``execute`` binding.
"""

import itertools
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from celery import Celery
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from due_work_harness.contract import _binding_proofs
from due_work_harness.host import Host, current_host, hosted
from due_work_harness.integrations.celery import celery_publications
from due_work_harness.integrations.django import django_host
from due_work_harness.integrations.django import lifecycle_references as ref
from due_work_harness.integrations.django.lifecycle_states import (
    TERMINAL_OBLIGATION_PROOFS,
    Lifecycle,
    _admitted_values,
    assert_every_lifecycle_state_is_declared,
    assert_terminal_states_owe_nothing_further,
)
from due_work_harness.profiles.automatic_recovery import (
    DUE_WORK_PROOFS,
    DueWorkSweep,
    OwedWorkVariant,
    assert_sweep_bindings_are_production_bound,
    assert_terminal_rows_are_never_selected,
)

Status = ref.Status
_NO_PROVIDER = "the reference worker only writes rows; it calls no provider"

#: A Celery app the publishing mutant hands its retry to. The host's publication
#: recorder holds every publication, so nothing reaches a broker.
_celery = Celery("reference-lifecycle", set_as_current=False)


@_celery.task(name="reference_lifecycle.reconcile_successor")
def _reconcile_successor(pk: int) -> None:
    ref.reconcile_attempt(pk)


@pytest.fixture
def attempt_table(transactional_db: None) -> Iterator[None]:
    with ref.lifecycle_attempt_table():
        yield


@pytest.fixture
def production_host() -> Iterator[Host]:
    """The Django host with the harness package as production, so a closure over the references reaches it."""
    with hosted(django_host({"due_work_harness"}, publication_recorder=celery_publications)) as host:
        yield host


def _make(status: str, *, age: timedelta) -> ref.LifecycleAttempt:
    # ARRANGE: persist one attempt and backdate it to simulate elapsed time.
    return ref.LifecycleAttempt.objects.create(status=status, updated_at=timezone.now() - age)


def _retryable_failed(classify: Callable[[int], None]) -> Callable[..., ref.LifecycleAttempt]:
    def make(*, age: timedelta) -> ref.LifecycleAttempt:
        # ARRANGE: a running attempt classified through the reference transition.
        attempt = _make(Status.RUNNING, age=age)
        classify(attempt.pk)
        attempt.refresh_from_db()
        return attempt

    return make


def _terminal(*, retryable: Callable[..., ref.LifecycleAttempt] | None) -> Callable[..., list[ref.LifecycleAttempt]]:
    def make(*, age: timedelta) -> list[ref.LifecycleAttempt]:
        rows = [_make(Status.COMPLETE, age=age), _make(Status.TERMINAL_FAILED, age=age)]
        return rows + ([retryable(age=age)] if retryable is not None else [])

    return make


def _recipient_calls(attempt: ref.LifecycleAttempt) -> int:
    # OBSERVE: notifications the external recipient received for this attempt.
    return ref.RECIPIENT.received.count(attempt.pk)


def _sweep(
    *,
    retryable: Callable[..., ref.LifecycleAttempt] | None,
    execute: Callable[[Any], Any] = ref.reconcile_attempt,
    excluded_states: dict[str, str] | None = None,
    state_fields: tuple[str, ...] = ("status",),
    lifecycle: bool = True,
    effect_calls_for: Callable[[Any], int] | None = None,
    no_external_effect_because: str | None = _NO_PROVIDER,
    publishes_nothing_because: str | None = None,
    variants: tuple[OwedWorkVariant, ...] | None = None,
) -> DueWorkSweep:
    return DueWorkSweep(
        name="reference attempt lifecycle",
        due_work=ref.due_for_recovery,
        run_tick=ref.run_recovery_tick,
        make_owed=lambda *, age: _make(Status.REQUESTED, age=age),
        additional_owed_variants=(OwedWorkVariant("running", lambda *, age: _make(Status.RUNNING, age=age)),)
        if variants is None
        else variants,
        make_terminal=_terminal(retryable=retryable),
        recovery_delay=ref.RECOVERY_DELAY,
        page_size=None,
        lifecycle=Lifecycle(
            state_fields=state_fields,
            execute=execute,
            excluded_states=excluded_states or {},
            effect_calls_for=effect_calls_for,
            no_external_effect_because=no_external_effect_because,
            publishes_nothing_because=publishes_nothing_because,
        )
        if lifecycle
        else None,
    )


def _reconcile_attempt_publishing_handoff(pk: int) -> None:
    """A retry handed off only as a task published through ``Task.delay``."""
    attempt = ref.LifecycleAttempt.objects.get(pk=pk)
    if attempt.status == Status.RETRYABLE_FAILED:
        _reconcile_successor.delay(pk)
        return
    ref.reconcile_attempt(pk)


def _reconcile_attempt_crashing(pk: int) -> None:
    raise RuntimeError(f"attempt {pk}: the worker failed before the application work")


# --- Composition ----------------------------------------------------------------


def test_the_django_host_runs_the_lifecycle_proofs_after_terminal_immunity() -> None:
    # They are not core proofs: the Django host supplies them, and the sweep
    # suite appends them after every core proof, invariant 2 included.
    assert not set(TERMINAL_OBLIGATION_PROOFS) & set(DUE_WORK_PROOFS)
    assert current_host().sweep_proofs == TERMINAL_OBLIGATION_PROOFS
    assert django_host(set(), lifecycle_proofs=False).sweep_proofs == ()
    proofs = _binding_proofs("sweep")
    assert proofs[-len(TERMINAL_OBLIGATION_PROOFS) :] == TERMINAL_OBLIGATION_PROOFS
    assert proofs.index(assert_terminal_rows_are_never_selected) < len(proofs) - len(TERMINAL_OBLIGATION_PROOFS)


# --- 2b and 2c, both directions -------------------------------------------------


def test_an_atomic_handoff_is_obligation_terminal(attempt_table: None) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure_atomically))
    assert_terminal_rows_are_never_selected(sweep)
    assert_every_lifecycle_state_is_declared(sweep)
    assert_terminal_states_owe_nothing_further(sweep)


def test_the_split_handoff_labelled_terminal_is_caught(attempt_table: None) -> None:
    # Invariant 2 passes, because the selection excludes the row; 2c does not.
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure))
    assert_terminal_rows_are_never_selected(sweep)
    assert_every_lifecycle_state_is_declared(sweep)
    with pytest.raises(AssertionError, match=r"still owe work.*retryable_failed.*created rows \['INSERT"):
        assert_terminal_states_owe_nothing_further(sweep)


@pytest.mark.parametrize(
    "execute", [ref.reconcile_attempt, ref.reconcile_attempt_under_lease], ids=["bare", "recovery-lease"]
)
def test_coordination_and_successor_progress_are_not_obligations(
    attempt_table: None, execute: Callable[[Any], Any]
) -> None:
    # The fixed shape: the successor commits with the failure, and a
    # redelivered predecessor advances it. Lease writes and that progress both
    # happen, and neither is debt a lost message could strand.
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure_atomically), execute=execute)
    assert_terminal_states_owe_nothing_further(sweep)


def test_the_split_handoff_behind_a_recovery_lease_is_still_caught(attempt_table: None) -> None:
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure), execute=ref.reconcile_attempt_under_lease
    )
    with pytest.raises(AssertionError, match=r"retryable_failed.*created rows"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_a_worker_that_reopens_a_terminal_row_is_caught(attempt_table: None) -> None:
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure_atomically), execute=ref.reconcile_attempt_reopening
    )
    with pytest.raises(AssertionError, match=r"moved the delivered row from \{'status': \{'complete'\}\}"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_the_split_handoff_left_undeclared_is_caught(attempt_table: None) -> None:
    # The main shape: the state is in neither make_terminal nor an owed variant.
    with pytest.raises(AssertionError, match=r"\['retryable_failed'\]\} are produced by no example"):
        assert_every_lifecycle_state_is_declared(_sweep(retryable=None))


def test_a_reasoned_exclusion_accounts_for_a_state(attempt_table: None) -> None:
    sweep = _sweep(
        retryable=None,
        excluded_states={"retryable_failed": "owned by the operator retry command and its own suite"},
    )
    assert_every_lifecycle_state_is_declared(sweep)


def test_a_field_scoped_exclusion_accounts_for_a_state(attempt_table: None) -> None:
    sweep = _sweep(
        retryable=None,
        excluded_states={"status:retryable_failed": "owned by the operator retry command and its own suite"},
    )
    assert_every_lifecycle_state_is_declared(sweep)


@pytest.mark.parametrize(
    ("excluded", "message"),
    [
        ({"retryable_failed": "later"}, "carry no real reason"),
        ({"retryable_failed": " " * 40}, "carry no real reason"),
        ({"paused": "a state the model does not have at all"}, "not production states"),
        ({"complete": "claimed to be owned by another proof"}, "either exercised or excluded"),
    ],
    ids=["thin-reason", "whitespace-reason", "unknown-state", "excluded-but-exercised"],
)
def test_exclusions_must_be_real_reasoned_and_unexercised(
    attempt_table: None, excluded: dict[str, str], message: str
) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure_atomically), excluded_states=excluded)
    with pytest.raises(AssertionError, match=message):
        assert_every_lifecycle_state_is_declared(sweep)


@pytest.mark.parametrize(
    "due_work", [ref.due_for_recovery, ref.due_for_recovery_by_exclusion], ids=["inclusion", "exclusion"]
)
def test_excluding_a_state_the_selection_selects_is_refused(attempt_table: None, due_work: Callable[[], Any]) -> None:
    # `.exclude(settled states)` admits RUNNING too; excluding it with prose would skip its OwedWorkVariant.
    sweep = replace(
        _sweep(
            retryable=_retryable_failed(ref.classify_retryable_failure_atomically),
            excluded_states={"running": "claimed to be owned by some other recovery path"},
            variants=(),
        ),
        due_work=due_work,
    )
    with pytest.raises(AssertionError, match=r"\{'status': \['running'\]\} are values the production selection"):
        assert_every_lifecycle_state_is_declared(sweep)


def test_a_state_field_the_selection_does_not_filter_is_refused(attempt_table: None) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure_atomically), state_fields=("retry_of",))
    with pytest.raises(AssertionError, match="does not filter on LifecycleAttempt.retry_of"):
        assert_every_lifecycle_state_is_declared(sweep)


@pytest.mark.parametrize("proof", TERMINAL_OBLIGATION_PROOFS, ids=lambda proof: proof.__name__)
def test_a_missing_lifecycle_is_a_refusal_not_a_pass(attempt_table: None, proof: Callable[[Any], None]) -> None:
    with pytest.raises(AssertionError, match="no `lifecycle=` declared"):
        proof(_sweep(retryable=None, lifecycle=False))


def test_a_selection_that_is_not_a_queryset_is_refused(attempt_table: None) -> None:
    sweep = replace(_sweep(retryable=None), due_work=lambda: list(ref.due_for_recovery()))
    with pytest.raises(AssertionError, match="must return a Django QuerySet, not list"):
        assert_every_lifecycle_state_is_declared(sweep)


# --- 2c's positive controls ------------------------------------------------------


def test_an_execution_that_never_reaches_the_work_fails_the_positive_control(attempt_table: None) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure), execute=lambda identity: None)
    with pytest.raises(AssertionError, match=r"positive control \(before the terminal deliveries\) failed"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_a_lease_claim_alone_does_not_satisfy_the_positive_control(attempt_table: None) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure), execute=ref.lease_only)
    with pytest.raises(AssertionError, match=r"positive control .*failed.*progress only"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_an_unexpected_worker_error_fails_the_positive_control(attempt_table: None) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure), execute=_reconcile_attempt_crashing)
    with pytest.raises(RuntimeError, match="before the application work") as raised:
        assert_terminal_states_owe_nothing_further(sweep)
    assert any("positive control (before the terminal deliveries)" in note for note in raised.value.__notes__)


def test_an_execution_that_reaches_the_work_only_once_fails_the_second_positive_control(attempt_table: None) -> None:
    calls = itertools.count()

    def first_call_only(pk: int) -> None:
        if next(calls) == 0:
            ref.reconcile_attempt(pk)

    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure), execute=first_call_only)
    with pytest.raises(AssertionError, match=r"positive control \(after the terminal deliveries\) failed"):
        assert_terminal_states_owe_nothing_further(sweep)


# --- Handoffs 2c must see however they are made -------------------------------------


@pytest.mark.parametrize(
    ("execute", "published"),
    [
        (_reconcile_attempt_publishing_handoff, r"published tasks \['reference_lifecycle.reconcile_successor'\]"),
        (ref.reconcile_attempt_sending_handoff, r"published tasks \['due_work_harness_references.reconcile'\]"),
    ],
    ids=["task-delay", "send-task-by-name"],
)
def test_a_published_handoff_is_caught(attempt_table: None, execute: Callable[[Any], Any], published: str) -> None:
    # `Celery.send_task` bypasses `Task.apply_async`; the publication recorder holds both.
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure), execute=execute)
    with pytest.raises(AssertionError, match=published):
        assert_terminal_states_owe_nothing_further(sweep)


def test_a_commit_callback_handoff_is_caught(attempt_table: None) -> None:
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure), execute=ref.reconcile_attempt_after_commit
    )
    # An enclosing transaction keeps the callback pending, as in a test-wrapped adopter suite.
    with transaction.atomic(), pytest.raises(AssertionError, match=r"1 on_commit callback"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_a_handoff_insert_is_seen_however_it_is_issued(attempt_table: None) -> None:
    # Classified by PostgreSQL's command status, not by the statement's text: a
    # data-modifying CTE behind a comment still counts as the INSERT it is.
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure), execute=ref.reconcile_attempt_with_cte_handoff
    )
    with pytest.raises(AssertionError, match=r"retryable_failed.*created rows \['INSERT"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_unparking_an_existing_successor_is_caught(attempt_table: None) -> None:
    # An UPDATE, not an INSERT: the successor exists but only the predecessor's
    # message makes it selectable, so a lost message parks it forever.
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure_with_parked_successor),
        execute=ref.reconcile_attempt_unparking,
    )
    with pytest.raises(AssertionError, match=r"retryable_failed.*made rows newly selectable by recovery"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_unparking_a_successor_behind_a_long_backoff_is_caught(attempt_table: None) -> None:
    # The successor becomes due six hours out, past the one-hour horizon the examples are aged by.
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure_with_parked_successor),
        execute=ref.reconcile_attempt_unparking_with_backoff,
    )
    with pytest.raises(AssertionError, match="newly selectable"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_a_handoff_only_for_fresh_failures_is_caught(attempt_table: None) -> None:
    # Aged examples alone would miss it: the worker ignores a failure older than its retry window.
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure), execute=ref.reconcile_attempt_within_retry_window
    )
    with pytest.raises(AssertionError, match="created rows"):
        assert_terminal_states_owe_nothing_further(sweep)


# --- External effects -----------------------------------------------------------------


def test_a_provider_call_on_a_terminal_delivery_is_caught(attempt_table: None) -> None:
    # No row, task or callback: only the provider fake can see this second effect.
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure_atomically),
        execute=ref.reconcile_attempt_calling_provider,
        effect_calls_for=_recipient_calls,
        no_external_effect_because=None,
    )
    with pytest.raises(AssertionError, match=r"'complete'.*1 external effect call\(s\) for this identity"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_provider_calls_only_for_active_work_pass(attempt_table: None) -> None:
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure_atomically),
        execute=ref.reconcile_attempt_calling_provider_for_active,
        effect_calls_for=_recipient_calls,
        no_external_effect_because=None,
    )
    assert_terminal_states_owe_nothing_further(sweep)


@pytest.mark.parametrize(
    ("observed", "reason"),
    [(False, None), (True, _NO_PROVIDER), (False, "none")],
    ids=["neither", "both", "thin-reason"],
)
def test_effect_observation_must_be_declared_exactly_once(
    attempt_table: None, observed: bool, reason: str | None
) -> None:
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure_atomically),
        effect_calls_for=_recipient_calls if observed else None,
        no_external_effect_because=reason,
    )
    with pytest.raises(AssertionError, match="effect_calls_for|no real reason"):
        assert_terminal_states_owe_nothing_further(sweep)


def test_an_effect_counter_that_never_counts_is_refused(attempt_table: None) -> None:
    # `lambda row: 0` would otherwise vouch that every terminal delivery calls nothing.
    sweep = _sweep(
        retryable=_retryable_failed(ref.classify_retryable_failure_atomically),
        execute=ref.reconcile_attempt_calling_provider_for_active,
        effect_calls_for=lambda _attempt: 0,
        no_external_effect_because=None,
    )
    with pytest.raises(AssertionError, match="effect_calls_for never counted a call"):
        assert_terminal_states_owe_nothing_further(sweep)


# --- Host capabilities ---------------------------------------------------------------


def test_without_a_publication_recorder_the_lifecycle_must_say_why_nothing_is_published(attempt_table: None) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure_atomically))
    with hosted(django_host(set())):
        with pytest.raises(AssertionError, match="host has no publication_recorder"):
            assert_terminal_states_owe_nothing_further(sweep)
        declared = _sweep(
            retryable=_retryable_failed(ref.classify_retryable_failure_atomically),
            publishes_nothing_because="the reference worker hands work off only through rows it writes",
        )
        assert_terminal_states_owe_nothing_further(declared)


def test_without_a_frozen_clock_2c_names_the_capability(attempt_table: None) -> None:
    sweep = _sweep(retryable=_retryable_failed(ref.classify_retryable_failure_atomically))
    with hosted(replace(current_host(), frozen_clock=None)), pytest.raises(AssertionError, match="'frozen_clock'"):
        assert_terminal_states_owe_nothing_further(sweep)


# --- The execute binding guard (0c) ------------------------------------------------------


def _guarded(execute: Callable[[Any], Any]) -> DueWorkSweep:
    # The selection and tick are harness-owned references, so only `execute` is under test.
    return _sweep(retryable=None, execute=execute)


def test_a_harness_owned_execution_passes_the_binding_guard() -> None:
    assert_sweep_bindings_are_production_bound(_guarded(ref.reconcile_attempt))


def test_a_test_authored_execution_is_refused_by_the_binding_guard() -> None:
    def authored(pk: int) -> None:
        ref.LifecycleAttempt.objects.filter(pk=pk).update(status=Status.RUNNING)

    with pytest.raises(AssertionError, match=r"lifecycle.execute authors production semantics"):
        assert_sweep_bindings_are_production_bound(_guarded(authored))


def test_an_in_memory_execution_is_refused_by_the_binding_guard() -> None:
    executed: list[int] = []
    with pytest.raises(AssertionError, match=r"lifecycle.execute is test code that references no production"):
        assert_sweep_bindings_are_production_bound(_guarded(lambda pk: executed.append(pk)))


def test_an_execution_that_ignores_its_identity_is_refused_unless_declared_tick_bound(
    production_host: Host,
) -> None:
    def ignores_identity(_pk: int) -> int:
        # Reaches production, but runs whatever the tick finds instead of the delivered attempt.
        return ref.run_recovery_tick()

    with pytest.raises(AssertionError, match=r"lifecycle.execute never reads its '_pk' parameter"):
        assert_sweep_bindings_are_production_bound(_guarded(ignores_identity))
    tick_bound = replace(
        _guarded(ignores_identity),
        lifecycle=Lifecycle(
            state_fields=("status",),
            execute=ignores_identity,
            inline_tick_because="the reference tick executes inline; no per-identity worker exists",
        ),
    )
    assert_sweep_bindings_are_production_bound(tick_bound)
    assert tick_bound.lifecycle is not None, "the tick-bound sweep declares a lifecycle"
    thin = replace(tick_bound, lifecycle=replace(tick_bound.lifecycle, inline_tick_because="inline"))
    with pytest.raises(AssertionError, match="inline_tick_because carries no real reason"):
        assert_sweep_bindings_are_production_bound(thin)


# --- Reading the selection's predicate ------------------------------------------------

_ALL_STATES = {str(choice) for choice, _label in Status.choices}


def _admitted(queryset: Any) -> set[str] | None:
    return _admitted_values(queryset.query.where, ref.LifecycleAttempt, "status", _ALL_STATES)


def test_admitted_values_intersect_and_union_like_the_predicate() -> None:
    # Building a queryset reads no rows, so this needs no database.
    # A broad active-state filter narrowed by an OR of states.
    everything = ref.LifecycleAttempt.objects.filter(status__in=[choice for choice, _label in Status.choices])
    narrowed = everything.filter(Q(status=Status.REQUESTED) | Q(status=Status.RUNNING))
    assert _admitted(narrowed) == {"requested", "running"}
    # An OR branch that does not constrain the field admits anything.
    assert _admitted(ref.LifecycleAttempt.objects.filter(Q(status=Status.REQUESTED) | Q(retry_of__isnull=True))) is None
    # A negation of state lookups admits their complement, and narrows an AND.
    assert _admitted(ref.LifecycleAttempt.objects.exclude(status=Status.COMPLETE)) == _ALL_STATES - {"complete"}
    both = ref.LifecycleAttempt.objects.filter(status__in=[Status.REQUESTED, Status.RUNNING])
    assert _admitted(both.exclude(status=Status.RUNNING)) == {"requested"}
    # A negation that mixes in another field (NOT a OR NOT b) constrains nothing.
    assert _admitted(ref.LifecycleAttempt.objects.exclude(status=Status.COMPLETE, retry_of__isnull=True)) is None
