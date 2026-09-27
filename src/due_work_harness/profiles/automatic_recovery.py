"""
Executable conformance contract for due-work recovery sweeps (profile A).

Many systems independently implement the same recovery shape: work is
dispatched after the transaction that records it commits, so a lost broker
message would strand it forever; a scheduled sweep re-selects rows that are
still outstanding and re-dispatches an idempotent worker. A recording retry, an
image-rendition backfill and an email campaign's stalled chunks are all
instances of it.

That recurrence is the interesting part, and it is why this module exists. It is
NOT a runtime, a base class, or a queue — each domain keeps its own tables,
states, and workers. It is the set of properties any such sweep must have,
written as assertions an adopter can run against its own implementation, so the
contract is checkable rather than described in prose.

The invariants, and why each one is load-bearing. The first ones are about the
adapter rather than the sweep, and everything after them depends on them:

0a. **The adapter forwards, it does not restate** — ``due_work`` calls the
   production selection instead of rebuilding the predicate in test code. An
   adapter that retypes it satisfies every other invariant here while describing
   a query nobody runs, and stays green forever after production drifts away
   from it.
0b. **The tick runs the selection the adapter describes** — ``due_work()`` and
   ``run_tick()`` are both bound by the adapter and were never compared. This
   catches the two halves diverging *inside* production, which 0a cannot.
0c. **Every semantic binding reaches production** — the selection, the tick and
   the lifecycle execution, not test-authored stand-ins for them.
0d. **The operator reading is read, not computed** — ``observe_outstanding``
   must reach production and must not aggregate the selection in the adapter.
   Invariant 9 compares that reading against the real backlog; an adapter that
   counts the backlog itself makes both sides of that comparison the same
   expression, so it matches forever while nothing is published.

1. **Rediscoverability** — work owed but never completed is selected once the
   recovery delay has passed. This is the whole point: it must not depend on a
   broker message surviving.
2. **Terminal immunity** — a row in any terminal state is never selected.
   Without this, a scheduled sweep is an infinite hot loop against work that
   will never succeed.
2b. **Every lifecycle state is declared** and 2c. **terminal states owe nothing
   further** — terminal must mean *obligation*-terminal, or invariant 2 hides a
   stranded handoff. Both read the model's own state choices and observe the
   database's writes, so they live in the Django integration
   (:mod:`due_work_harness.integrations.django.lifecycle_states`) and are not in
   :data:`DUE_WORK_PROOFS`; a Django adopter composes them in.
3. **No duplicate in-flight work** — the proof starts the real production
   execution, holds it open, and runs a competing recovery tick. The same
   logical identity must reach the effect exactly once. A fresh row inside a
   time delay is not accepted as evidence that anything is in flight.
4. **Boundedness** — one tick dispatches at most ``page_size`` rows, so a
   backlog drains over several ticks instead of flooding a queue in one.
5. **Index-served selection** — an index actually *narrows* the selection.
   This is the invariant that is invisible in review and fails silently: the
   query runs on a schedule forever, so an unpruned selection becomes a periodic
   full scan of a production table. Checking merely for the absence of a
   sequential scan is not enough; the plan verdict explains why.
6. **No starvation** — the oldest owed work is selected first.
7. **Stability** — two evaluations with no intervening writes agree.
8. **Something runs the tick** — executable evidence of a recurring schedule.
9. **Observability** — an operator-visible reading tracks the real backlog.
10. **Primary reads** — due work is never selected from a lagging replica.
11. **Ambient context is restored** — a tick leaves per-call context (the
    current tenant, for example) as it found it. Opt-in; see below.

Proofs are framework-free. ``due_work`` returns any iterable of rows; proofs
materialise it with ``list(...)`` and compare rows through
:attr:`DueWorkSweep.identity_of`. The facts only a database can answer — which
plan serves the selection, which database it reads, how many rows it discards,
how many statements a tick issues — are asked of the configured host through
``current_host().inspector_for(selection, capability=...)``; with no inspector
that understands the selection, those proofs fail naming the missing capability
rather than guessing.

Invariant 11 needs the host's ``ambient_context``. It is deliberately not in
:data:`DUE_WORK_PROOFS`: a host that defines no ambient context has nothing to
restore, and a proof that passed silently there would claim a tenancy guarantee
nobody checked. Add :data:`AMBIENT_CONTEXT_PROOFS` when your host defines one;
the proof fails, naming the capability, if it is applied to a host that does not.

Usage — implement :class:`DueWorkSweep` for the domain and call
:func:`assert_due_work_recovery_contract`:

    sweep = DueWorkSweep(
        name="invoice reminders",
        # Forward to the production selection. Building the predicate here
        # instead is what invariant 0a rejects: it is a second description of
        # the query, and it drifts silently.
        due_work=lambda: Invoice.objects.due_for_reminder_recovery(cutoff),
        run_tick=recover_invoice_reminders_task,
        make_owed=...,
        make_terminal=...,
        recovery_delay=REMINDER_RECOVERY_GRACE,
        page_size=REMINDER_RECOVERY_PAGE_SIZE,
        dispatched_ids=lambda: [pk for pk, _source in dispatched],
        in_flight=InFlightExecution(
            make_owed=...,
            # Receives the harness-supplied production run_tick callback.
            start=hold_recovery_tick_effect_open,
            execution_count_for=...,
        ),
    )
    assert_due_work_recovery_contract(sweep)

Each proof is also exported individually, because an adopter that cannot satisfy
one of them should xfail exactly that invariant with a reason rather than skip
the contract wholesale.

Most adopters are partial on purpose, and there are two ways to say so. Callable
members that no applied proof reaches take
:func:`~due_work_harness.helpers.undeclared`, which carries the reason to
whoever later broadens the adopter and reaches one. ``recovery_delay`` and
``page_size`` take ``None``. Both exist because the alternative is inventing a
value: adopters have declared a ``page_size`` they did not have, including one
whose own documentation said there was no page size to declare. A fabricated
number is inert only until someone applies a proof that reads it, and then it
measures something nobody described. ``recovery_delay`` describes only when
selection begins; invariant 3 independently requires a production-started
``InFlightExecution`` for every delay value, including zero.
"""

from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import timedelta
from itertools import islice
from types import ModuleType
from typing import Any, Protocol
from unittest import mock

from pydantic import SkipValidation

from due_work_harness.binding import (
    BACKLOG_AUTHORING_OPERATIONS,
    INVOCATION_AUTHORING_OPERATIONS,
    SELECTION_AUTHORING_OPERATIONS,
    assert_binding_reaches_production,
    assert_test_binding_consumes_its_first_parameter,
    assert_test_binding_delegates_to_production,
    assert_test_binding_forwards,
    is_real_reason,
)
from due_work_harness.host import SelectionInspector, current_host
from due_work_harness.models import HarnessModel


class OwedWorkVariant(HarnessModel):
    """One additional production-reachable shape that the sweep must recover."""

    name: str
    make: Callable[..., Any]


class InFlightExecution(HarnessModel):
    """
    Recovery-tick-started work held open while a second tick competes.

    ``make_owed`` arranges a recovery-eligible identity. The harness passes its
    instrumented production ``run_tick`` into ``start``; ``start`` must invoke
    that callback exactly once before yielding and keep the resulting dispatch
    or inline execution outstanding for the duration of the ``with`` block.
    This inversion matters: starting a downstream worker directly can acquire
    a claim and hide duplicate publications that occurred before the worker
    began. ``execution_count_for`` observes how many times the same identity
    reached the recovery tick's first externally meaningful effect boundary.

    The proof requires the observation to move ``0 -> 1 -> 1`` around a
    competing recovery tick. That positive control rejects a no-op start and
    the final equality rejects both redispatch and concurrent execution.
    """

    make_owed: Callable[[], Any]
    start: Callable[[Any, Callable[[], int]], AbstractContextManager[None]]
    execution_count_for: Callable[[Any], int]


def _default_identity(row: Any) -> Any:
    """
    A row's ``pk`` when it has one, otherwise the row itself.

    ORM rows are compared by primary key, which is also what a dispatch
    recorder usually emits; a selection of plain identities is its own identity.
    Override :attr:`DueWorkSweep.identity_of` for anything else.
    """
    return getattr(row, "pk", row)


class SelectionAdapter(Protocol):
    """
    The subset of a due-work adapter the selection-only proofs need.

    :func:`assert_the_adapter_does_not_author_the_selection`,
    :func:`assert_selection_is_index_served` and
    :func:`assert_selection_does_not_read_the_replica` read nothing but a name
    and the selection, so they are equally applicable to a scheduled query that
    is not a full due-work sweep — that is what
    :class:`due_work_harness.contract.ScheduledSelection` binds. Profile A
    composes the same three proofs into :data:`DUE_WORK_PROOFS`.
    """

    @property
    def name(self) -> str: ...

    @property
    def due_work(self) -> Callable[[], Iterable[Any]]: ...


class DueWorkSweep(HarnessModel):
    """
    One domain's recovery sweep, described so the proofs can exercise it.

    ``make_owed`` and ``make_terminal`` are the only domain-specific setup the
    contract needs. Keeping them as callables is what lets each adopter build
    rows with its own factories and state names instead of conforming to a
    shared model.
    """

    #: Human-readable adopter name, used in assertion messages.
    name: str

    #: The unbounded due-work selection, exactly as the production sweep builds
    #: it (including its recovery-delay filter), in the order production reads
    #: it. Any iterable of rows; proofs that need database facts about it (its
    #: plan, the database it reads) pass the returned object to the host's
    #: selection inspector, so return the framework's own query object — a
    #: Django QuerySet, for example — rather than a list.
    due_work: Callable[[], Iterable[Any]]

    #: Execute one tick. Returns the number of rows dispatched.
    run_tick: Callable[[], int]

    #: Create one row that genuinely owes work, aged by ``age`` into the past.
    make_owed: Callable[..., Any]

    #: Create one row in every terminal state the domain has, aged into the
    #: past. Returns the created rows.
    make_terminal: Callable[..., list[Any]]

    #: How long owed work must age before the recovery selection may see it.
    #: This is an eligibility/latency value, never evidence that an execution is
    #: in flight. ``None`` when the adopter genuinely has no value to declare
    #: and applies only proofs that do not need one — see :func:`undeclared`.
    recovery_delay: timedelta | None

    #: Maximum rows one tick may dispatch. ``None`` when the adopter has no page
    #: to declare — a tick that claims and runs work inline rather than fanning
    #: it out has no page, and declaring ``page_size=0`` instead says something
    #: false.
    page_size: int | None

    #: Executable evidence that something runs this tick recurringly. Raise to
    #: fail. Scheduler-neutral on purpose: an adopter on a workflow engine, an
    #: external scheduler, or structural enrolment behind one generic task can
    #: supply its own evidence rather than being rejected for not using a
    #: particular scheduler.
    #: :func:`due_work_harness.integrations.celery.celery_beat_evidence` covers
    #: Celery beat.
    assert_scheduled: Callable[[], None] | None = None

    #: How often the tick runs. Required by the latency and drain proofs, which
    #: otherwise have to guess.
    #: :func:`due_work_harness.integrations.celery.celery_beat_interval` reads it
    #: from a Celery beat schedule.
    tick_interval: timedelta | None = None

    #: A real execution held open while the recovery tick competes. Required by
    #: the no-duplicate proof regardless of whether production uses a redispatch
    #: grace, a transaction lock, or another mechanism. ``None`` means the
    #: adopter has not supplied evidence for that invariant; it never means the
    #: invariant is waived.
    in_flight: InFlightExecution | None = None

    #: An operator-visible reading of how much work is currently outstanding —
    #: a Prometheus gauge, a health field, anything queryable from outside the
    #: process. Declaring it enables
    #: :func:`assert_outstanding_work_is_observable`.
    observe_outstanding: Callable[[], float | None] | None = None

    #: The row identifiers the most recent tick dispatched, in dispatch order.
    #: Adopters typically already record these to assert on them — a patched
    #: dispatch path appending what it sent — so declaring it costs a line and
    #: is what lets :func:`assert_the_tick_selects_what_the_adapter_describes`
    #: compare the adapter's two halves against each other instead of trusting
    #: both. ``None`` when the adopter does not apply that proof. It must be a
    #: *recorder* (a patched dispatch path appending what production sent), not
    #: a reader of the selection — that proof verifies the distinction by
    #: requiring it to be empty before the tick runs.
    dispatched_ids: Callable[[], list[Any]] | None = None

    #: Additional ways production can owe this same work. The representative
    #: ``make_owed`` drives every invariant; these variants mechanically prove
    #: that every declared predicate branch is also selected and dispatched.
    additional_owed_variants: tuple[OwedWorkVariant, ...] = ()

    #: Convert one row — selected, or made by ``make_owed``/``make_terminal`` —
    #: to the identity emitted by ``dispatched_ids``. Every proof compares rows
    #: through it.
    identity_of: Callable[[Any], Any] = _default_identity

    #: The production state field(s) and per-identity execution. Required by
    #: invariants 2b and 2c (in the Django integration): every lifecycle state
    #: must be owed, terminal, or excluded with a reason, and every terminal
    #: state must owe nothing more. Invariant 0c checks its execution binding
    #: here whenever one is declared.
    #:
    #: A :class:`~due_work_harness.integrations.django.lifecycle_states.Lifecycle`
    #: or ``None``. Typed ``Any`` because ``Lifecycle`` lives in the Django
    #: integration, which the framework-free core never imports, and a Pydantic
    #: field cannot name a type imported only for the type checker.
    lifecycle: SkipValidation[Any] = None


def declared_recovery_delay(sweep: DueWorkSweep) -> timedelta:
    """
    The sweep's declared recovery eligibility delay.

    A contract assertion rather than a fallback: a proof that quietly treated an
    undeclared delay as zero would age its rows wrongly and report a result
    about a sweep nobody described.
    """
    assert sweep.recovery_delay is not None, (
        f"{sweep.name}: this proof ages rows relative to the recovery delay, and "
        f"the adopter declared none. Pass `recovery_delay=` — or apply only the proofs "
        f"that do not need one"
    )
    return sweep.recovery_delay


#: Rows per tick while proving the recovery bound. The bound is "a tick selects
#: at most the page size", which holds or fails the same way at any page size,
#: but proving it builds ``page_size + 3`` genuine rows through the adopter's
#: factories. Production pages of 50-500 rows made each proof cost ~2-20 s.
CONTRACT_PAGE_SIZE = 5


def with_contract_page_size(
    sweep: Callable[[], AbstractContextManager[DueWorkSweep]],
    owner: ModuleType,
    constant: str,
) -> Callable[[], AbstractContextManager[DueWorkSweep]]:
    """
    Run an adopter's sweep with its production page-size constant shrunk.

    ``owner.constant`` must be the module global the production tick reads at
    call time, and the adapter must declare ``page_size=owner.constant`` so the
    declared and the enforced page are the same value. A tick that ignored the
    constant would dispatch more than the shrunk page and fail the bound proofs.
    """

    @contextmanager
    def shrunk() -> Iterator[DueWorkSweep]:
        page_size = min(getattr(owner, constant), CONTRACT_PAGE_SIZE)
        with mock.patch.object(owner, constant, page_size), sweep() as built:
            assert built.page_size == page_size, (
                f"{built.name}: declared page_size={built.page_size} but production reads "
                f"{owner.__name__}.{constant}={page_size}. Declare `page_size={owner.__name__}.{constant}` "
                f"so the proof measures the page the tick enforces"
            )
            yield built

    return shrunk


def _page_size(sweep: DueWorkSweep) -> int:
    """The sweep's declared page size."""
    assert sweep.page_size is not None, (
        f"{sweep.name}: this proof measures the per-tick bound, and the adopter "
        f"declared no page size. Pass `page_size=` — or apply only the proofs "
        f"that do not need one"
    )
    return sweep.page_size


def _selected_identities(sweep: DueWorkSweep, *, limit: int | None = None) -> list[Any]:
    """One evaluation of the production selection, in its order, as identities."""
    return [sweep.identity_of(row) for row in islice(sweep.due_work(), limit)]


def _inspector(selection: object, capability: str) -> SelectionInspector:
    return current_host().inspector_for(selection, capability=capability)


def _statement_inspector(capability: str, selection: object | None) -> SelectionInspector:
    """
    The inspector that counts a tick's statements.

    Statement counting is a property of the connection rather than of one
    selection, so a proof that has no selection uses the host's only inspector;
    with several configured, the caller names the selection whose database is
    meant.
    """
    if selection is not None:
        return _inspector(selection, capability)
    inspectors = current_host().selection_inspectors
    assert len(inspectors) == 1, (
        f"{capability} counts database statements and needs exactly one SelectionInspector to ask, "
        f"and the configured host has {len(inspectors)}. "
        + (
            "Pass `selection=` to choose the database whose statements are counted"
            if inspectors
            else "Use an integration's host (for example due_work_harness.integrations.django.django_host) "
            "or add an inspector"
        )
    )
    return inspectors[0]


def assert_the_adapter_does_not_author_the_selection(sweep: SelectionAdapter) -> None:
    """
    INVARIANT 0a: ``due_work`` forwards to production, it does not restate it.

    Every other proof in this module is only as true as this one. An adapter
    that retypes the production predicate satisfies every behavioural invariant
    while describing a query nobody runs, and it keeps reporting green after
    production drifts away from it. A harness which merely *says* the adapter
    must use the production selection cannot enforce it; this is the
    enforcement.

    Checked by the shared semantic-binding tripwire. A forwarding lambda is
    legitimately defined in the test module; what is not legitimate is a
    lambda whose body *is* the predicate. The check follows referenced
    test-authored helpers recursively, so moving the copy into a local helper
    does not hide it::

        lambda: Model.objects.due_for_x(cutoff)   -> ('Model', 'objects',
                                                      'due_for_x', 'cutoff')
        lambda: Model.objects.filter(Q(...))      -> (..., 'filter', 'Q')

    Ordering counts as authorship too, and deliberately: invariants 6 and 7
    assert on the selection's order, so an ``order_by`` applied in the adapter
    means those proofs measure an ordering production does not have.

    A focused authorship heuristic, not a general program-equivalence proof. It pairs with
    :func:`assert_the_tick_selects_what_the_adapter_describes`, which is exact
    but needs a fully populated adapter — this one needs no rows and no
    factories, so it still fires on a partial adopter that declines the rest of
    the contract with :func:`~due_work_harness.helpers.undeclared`.
    """
    assert_test_binding_forwards(
        adopter=sweep.name,
        field="due_work",
        binding=sweep.due_work,
        forbidden=SELECTION_AUTHORING_OPERATIONS,
        production_shape="a production selection",
    )


def assert_the_adapter_does_not_author_the_tick(sweep: DueWorkSweep) -> None:
    """INVARIANT 0b: the tick invokes production instead of reproducing it."""
    assert_test_binding_forwards(
        adopter=sweep.name,
        field="run_tick",
        binding=sweep.run_tick,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the production recovery tick",
    )


def _first_divergence(described: list[Any], actual: list[Any]) -> str:
    """
    Where two selections first disagree, and a window around it.

    Production page sizes run to hundreds of rows, so printing both lists in
    full buries the one row that matters among hundreds that match. The index
    and a short window are what a reader needs to tell a predicate change from
    an ordering change from a bound change.
    """
    if len(described) != len(actual):
        length_note = f"The tick dispatched {len(actual)} rows against {len(described)} described. "
    else:
        length_note = ""
    for index, (want, got) in enumerate(zip(described, actual, strict=False)):
        if want != got:
            window = slice(max(0, index - 2), index + 3)
            return (
                f"{length_note}First disagreement at position {index}: the "
                f"adapter describes {want!r}, the tick dispatched {got!r} "
                f"(described[{window.start}:{window.stop}]="
                f"{described[window]!r} vs actual={actual[window]!r})."
            )
    shorter, longer = ("adapter's", "tick's") if len(described) < len(actual) else ("tick's", "adapter's")
    extra = actual[len(described) :] or described[len(actual) :]
    return (
        f"{length_note}They agree for the whole of the {shorter} selection; the {longer} continues with {extra[:5]!r}."
    )


def assert_the_tick_selects_what_the_adapter_describes(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 0b: ``due_work()`` is the selection ``run_tick()`` actually runs.

    The adapter supplies both halves and nothing has ever compared them. This is
    the move :func:`assert_outstanding_work_is_observable` already makes for the
    observability reading — compare a declaration against reality rather than
    against itself — pointed at the declaration every other proof depends on.

    It is a differential assertion, not an expectation: the expected value is
    not a list written in the test but the result of evaluating the adapter's
    *other* production binding. Nobody authors the truth, which is what
    separates it from ``mock.assert_has_calls`` and its stale literals.

    What it does NOT catch: if ``due_work`` is a copy that happens to be
    accurate today, both halves agree and this passes. It detects divergence,
    not transcription — which is why 0a runs first and establishes that the two
    halves are two production paths rather than one path and one copy of it.
    """
    assert sweep.dispatched_ids is not None, (
        f"{sweep.name}: no dispatched_ids supplied, so the selection the tick "
        f"actually runs cannot be compared against the one the adapter "
        f"declares. Pass `dispatched_ids=` — most adopters already record this"
    )
    recovery_delay = declared_recovery_delay(sweep)
    page_size = _page_size(sweep)

    for index in range(page_size + 3):
        sweep.make_owed(age=recovery_delay + timedelta(minutes=index + 1))
    sweep.make_terminal(age=recovery_delay + timedelta(hours=1))
    # Inside the recovery delay: described by neither half, so its presence in
    # either would be a disagreement this proof should report.
    sweep.make_owed(age=timedelta(0))

    premature = list(sweep.dispatched_ids())
    assert premature == [], (
        f"{sweep.name}: dispatched_ids() reported {len(premature)} row(s) "
        f"before any tick ran. A recorder of what production dispatched is "
        f"empty here; a binding that already reports rows is reading the "
        f"selection instead of recording dispatch, which turns this proof "
        f"into the selection compared against itself"
    )
    # `identity_of`, not the raw row: the adapter declares how a selected row
    # maps to the identity `dispatched_ids` emits, and every other proof in this
    # module already reads it. Comparing raw rows against a different dispatch
    # identity reports a divergence between the adapter's own two halves that
    # neither half has — the accusing direction this package treats as worse
    # than silence — and pressures the adopter into making `dispatched_ids`
    # contradict `identity_of` to get green.
    described = _selected_identities(sweep, limit=page_size)
    sweep.run_tick()
    actual = list(sweep.dispatched_ids())

    assert actual == described, (
        f"{sweep.name}: the tick's selection and the adapter's disagree. "
        f"{_first_divergence(described, actual)} Every other proof in this "
        f"contract is measuring a selection the tick does not run"
    )


def assert_sweep_bindings_are_production_bound(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 0c: the selection AND the tick reach production code.

    Invariant 0a rejects a ``due_work`` that authors query semantics in test
    code; it cannot reject a binding that authors nothing because it touches
    no query API at all. Nothing else examines ``run_tick``: a test-authored
    tick that evaluates ``due_work()`` and feeds the recorder satisfies 0b *by
    construction* — the differential proof compares the selection against
    itself — and every behavioral invariant then certifies a tick production
    never runs. Both bindings must therefore reach production code; a
    forwarding wrapper (a service resolved and called, a task invoked) does, a
    self-contained re-implementation does not.
    """
    assert_test_binding_delegates_to_production(
        adopter=sweep.name,
        field="due_work",
        binding=sweep.due_work,
        production_shape="the production selection",
    )
    assert_test_binding_delegates_to_production(
        adopter=sweep.name,
        field="run_tick",
        binding=sweep.run_tick,
        production_shape="the production tick — the scheduled task or the service method it calls",
    )
    if sweep.observe_outstanding is not None:
        assert_the_backlog_reading_is_production_published(sweep)
    assert_lifecycle_execution_is_production_bound(sweep)


def assert_lifecycle_execution_is_production_bound(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 0c for ``lifecycle.execute``: it invokes the production worker, not a copy, on its identity.

    Framework-free, so it lives here rather than beside the lifecycle-state
    proofs: the binding guard applies to every declared lifecycle, whether or
    not the host can run 2b and 2c.
    """
    lifecycle = sweep.lifecycle
    if lifecycle is None:
        return
    assert_binding_reaches_production(
        adopter=sweep.name,
        field="lifecycle.execute",
        binding=lifecycle.execute,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the per-identity worker the recovery tick dispatches",
    )
    if lifecycle.inline_tick_because is None:
        assert_test_binding_consumes_its_first_parameter(
            adopter=sweep.name,
            field="lifecycle.execute",
            binding=lifecycle.execute,
            parameter_shape="the delivered identity",
        )
    else:
        assert is_real_reason(lifecycle.inline_tick_because), (
            f"{sweep.name}: inline_tick_because carries no real reason; say why no per-identity worker exists"
        )


def assert_the_backlog_reading_is_production_published(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 0d: the operator reading is read from production, not computed here.

    :func:`assert_outstanding_work_is_observable` compares the published number
    against the real backlog, and that comparison is only meaningful when the
    two sides are independent authorities. They are not when the adapter
    *computes* the reading: ``lambda: float(Model.objects.due_for_x(cutoff).count())``
    matches the backlog by construction, forever, while nothing outside the
    process publishes anything and a stalled sweep stays exactly as invisible
    as invariant 9 says it must not be.

    That counterfeit is cheap and invited. An adopter whose invariant 9 is a
    recorded gap naturally names its fix as "a gauge of the backlog count
    published every tick"; binding the count without the publish deletes the
    gap and passes.

    Two tripwires, the same duals used on every other semantic field:

    * the binding must reach production code — a metric registry read, a
      service method, a health field — rather than closing over test state;
    * the binding must not aggregate the selection itself, which is what
      "computes the reading" looks like in an adapter.

    ``dispatched_ids`` deliberately gets neither. It is documented as a
    *recorder* — a patched dispatch path appending what production sent — so a
    plain list is its intended shape, and its independence is already
    established by the ``premature == []`` check inside invariant 0b.
    """
    binding = sweep.observe_outstanding
    assert binding is not None, f"{sweep.name}: no backlog reading is bound"
    assert_binding_reaches_production(
        adopter=sweep.name,
        field="observe_outstanding",
        binding=binding,
        forbidden=BACKLOG_AUTHORING_OPERATIONS,
        production_shape="a production reading of the outstanding backlog — the metric the tick publishes, or the health field that serves it",
    )


def assert_recovers_stranded_work(sweep: DueWorkSweep) -> None:
    """INVARIANT 1: work owed past the recovery delay is rediscovered."""
    owed = sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(minutes=1))
    selected = _selected_identities(sweep)
    assert sweep.identity_of(owed) in selected, (
        f"{sweep.name}: a row owing work for longer than the recovery delay was "
        f"not selected, so a lost dispatch would strand it permanently"
    )
    assert sweep.run_tick() >= 1, f"{sweep.name}: the selection found stranded work but the tick dispatched nothing"


def assert_all_owed_variants_are_recovered(sweep: DueWorkSweep) -> None:
    """INVARIANT 1b: every declared owed-state branch reaches the real tick."""
    if not sweep.additional_owed_variants:
        return
    assert sweep.dispatched_ids is not None, (
        f"{sweep.name}: additional owed variants are declared but dispatched_ids is missing, "
        "so the contract cannot prove the production tick consumed each variant"
    )
    variants = (OwedWorkVariant(name="representative", make=sweep.make_owed), *sweep.additional_owed_variants)
    # One page per tick: an exhaustive variant list can exceed the page. Each
    # chunk is older than the last, so an oldest-first tick (invariant 6)
    # serves it ahead of earlier chunks that stay eligible after dispatch.
    page = sweep.page_size or len(variants)
    for start in range(0, len(variants), page):
        created = {
            variant.name: variant.make(age=declared_recovery_delay(sweep) + timedelta(minutes=start + index + 1))
            for index, variant in enumerate(variants[start : start + page])
        }
        selected = set(_selected_identities(sweep))
        missing_selection = {
            name: sweep.identity_of(row) for name, row in created.items() if sweep.identity_of(row) not in selected
        }
        assert not missing_selection, (
            f"{sweep.name}: declared owed variants were absent from the production selection: "
            f"{missing_selection}. Each predicate branch needs a representative variant"
        )

        sweep.run_tick()
        dispatched = set(sweep.dispatched_ids())
        missing_dispatch = {
            name: sweep.identity_of(row) for name, row in created.items() if sweep.identity_of(row) not in dispatched
        }
        assert not missing_dispatch, (
            f"{sweep.name}: the production tick selected but did not dispatch declared owed variants: "
            f"{missing_dispatch}"
        )


def assert_terminal_rows_are_never_selected(sweep: DueWorkSweep) -> None:
    """INVARIANT 2: no terminal state is ever re-dispatched."""
    terminal = sweep.make_terminal(age=declared_recovery_delay(sweep) + timedelta(hours=1))
    assert terminal, (
        f"{sweep.name}: make_terminal produced no rows, so this proof would "
        f"pass vacuously; return one row per terminal state"
    )
    selected = set(_selected_identities(sweep))
    offenders = [row for row in terminal if sweep.identity_of(row) in selected]
    assert not offenders, (
        f"{sweep.name}: terminal rows {offenders!r} were selected for "
        f"re-dispatch. A scheduled sweep that re-dispatches terminal work is an "
        f"unbounded hot loop against work that will never succeed"
    )


def assert_in_flight_work_is_not_duplicated(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 3: two workers do not run the same row at once.

    The proof must enter the state it names. A fresh row is not evidence of an
    in-flight execution: an initial eligibility delay can exclude that row even
    when nothing has started. Starting the worker directly is insufficient too:
    its claim can hide a duplicate-publication window before execution began.
    The harness therefore supplies the real production recovery tick, requires
    ``start`` to invoke it exactly once, and holds that result open while a
    second tick competes.

    The execution-count observation is checked before start, after start, and
    after the competing tick. ``0 -> 1 -> 1`` is both the positive control that
    the first execution genuinely began and the negative proof that recovery
    did not execute the same logical work again.
    """
    in_flight = sweep.in_flight
    assert in_flight is not None, (
        f"{sweep.name}: no production-started in-flight execution is bound, so "
        "the contract cannot prove that recovery avoids duplicating work that "
        "is actually running. Bind `in_flight=` with a production `start`, an "
        "execution-count observation, and a context that holds the first run open"
    )

    row = in_flight.make_owed()
    identity = sweep.identity_of(row)
    eligible = set(_selected_identities(sweep))
    assert identity in eligible, (
        f"{sweep.name}: InFlightExecution.make_owed created identity {identity!r}, "
        "but it was not recovery-eligible before start. A fresh or otherwise "
        "excluded row cannot prove what the recovery tick does to outstanding work"
    )
    before = in_flight.execution_count_for(row)
    assert before == 0, (
        f"{sweep.name}: execution_count_for reported {before} execution(s) before "
        "start. The observer cannot establish the positive control that the "
        "production start caused the execution"
    )
    recovery_tick_starts = 0

    def start_recovery_tick() -> int:
        nonlocal recovery_tick_starts
        recovery_tick_starts += 1
        return sweep.run_tick()

    with in_flight.start(row, start_recovery_tick):
        assert recovery_tick_starts == 1, (
            f"{sweep.name}: in_flight.start invoked the production recovery tick "
            f"{recovery_tick_starts} times before yielding; it must invoke the "
            "harness-supplied tick exactly once rather than start a downstream worker directly"
        )
        started = in_flight.execution_count_for(row)
        assert started == 1, (
            f"{sweep.name}: production start did not produce exactly one observable "
            f"execution for the owed identity; observed {started}"
        )
        sweep.run_tick()
        after_competitor = in_flight.execution_count_for(row)
        assert after_competitor == 1, (
            f"{sweep.name}: the same logical work executed {after_competitor} times "
            "while the first execution was still in flight"
        )


def assert_published_work_is_recoverable(
    sweep: DueWorkSweep,
    *,
    source_name: str,
    publish: Callable[[], Any],
    make_recovery_eligible: Callable[[Any], None],
) -> None:
    """
    The sweep recovers what the covered publisher leaves behind.

    ``covers=(DueWorkSource(...),)`` is the whole reason a contract names its
    publishers: this callable publishes work after a commit, so a dead broker
    message strands it, and THIS contract is the insurance. A static tripwire
    can check that the association exists — matching qualified names and
    counting after-commit hooks — but never what it means. A contract could
    name one publisher and recover an entirely different table.

    So the loop is closed the way the A-F coherence proof closes its own: run
    the real publisher with its dispatch lost, then require the real selection
    to find the row and the real tick to dispatch it.

    The empty-dispatch check after ``publish`` is the positive control. If the
    publisher's own dispatch still fires, nothing was lost, and the proof would
    "recover" work that was never stranded — which is the reading that makes a
    broken sweep look sound.
    """
    assert sweep.dispatched_ids is not None, (
        f"{sweep.name}: covered source {source_name} declares a publisher, but the sweep has no "
        f"dispatched_ids observation, so the contract cannot prove recovery consumed what it published"
    )

    identity = publish()
    leaked = list(sweep.dispatched_ids())
    assert leaked == [], (
        f"{sweep.name}: publishing {source_name} dispatched {len(leaked)} row(s). This proof is "
        f"about a LOST dispatch, so the publisher must run with its own dispatch suppressed — "
        f"otherwise the work was never stranded and recovering it proves nothing"
    )

    make_recovery_eligible(identity)
    selected = set(_selected_identities(sweep))
    assert identity in selected, (
        f"{sweep.name}: {source_name} publishes work after a commit, and this contract is what "
        f"insures it against a lost message — but the recovery selection never sees the row it "
        f"leaves behind ({identity!r}). The contract associates the two; nothing else checks that "
        f"the association means anything"
    )

    sweep.run_tick()
    dispatched = set(sweep.dispatched_ids())
    assert identity in dispatched, (
        f"{sweep.name}: the recovery selection found {source_name}'s stranded work ({identity!r}) "
        f"and the tick did not dispatch it"
    )


def assert_tick_is_bounded(sweep: DueWorkSweep) -> None:
    """INVARIANT 4: one tick cannot flood the queue."""
    page_size = _page_size(sweep)
    for index in range(page_size + 3):
        sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(minutes=index + 1))
    dispatched = sweep.run_tick()
    assert dispatched == page_size, (
        f"{sweep.name}: a tick dispatched {dispatched} rows against a declared "
        f"page size of {page_size}; a backlog must drain over several "
        f"ticks rather than be enqueued at once"
    )


def assert_oldest_work_is_recovered_first(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 6: no starvation.

    A bounded tick plus an unbounded backlog means selection order decides who
    is ever served. Newest-first, or unordered, lets the oldest work sit behind
    a continuously refilled queue forever — the failure is invisible until a
    backlog exists.
    """
    newer = sweep.identity_of(sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(minutes=1)))
    older = sweep.identity_of(sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(days=2)))

    selected = _selected_identities(sweep)
    missing = [identity for identity in (older, newer) if identity not in selected]
    assert not missing, (
        f"{sweep.name}: owed rows {missing!r} are absent from the selection, so their order cannot be compared"
    )
    assert selected.index(older) < selected.index(newer), (
        f"{sweep.name}: the newer row is selected before the older one, so a "
        f"backlog larger than one tick can starve the oldest work indefinitely"
    )


def assert_selection_is_stable(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 7: the selection is deterministic.

    Two evaluations with no intervening writes must agree. An unstable
    selection — ties broken arbitrarily, or a non-deterministic ordering — makes
    both the bound and the ordering meaningless, because each tick can see a
    different slice of the same backlog.
    """
    for index in range(3):
        sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(minutes=index + 1))

    first = _selected_identities(sweep)
    second = _selected_identities(sweep)
    assert first == second, (
        f"{sweep.name}: two evaluations of the due-work selection disagreed, so "
        f"the page bound and the ordering guarantee are both unreliable"
    )


def assert_the_tick_is_actually_scheduled(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 8: something actually runs the sweep, recurringly.

    Every other invariant in this profile assumes the tick executes. None of
    them notices if nobody wired it up, so a flawless sweep that no scheduler
    calls passes the entire contract while recovering nothing. That is not
    hypothetical: a sweep can be written, reviewed and merged with no schedule
    entry at all, and its row-level guards satisfy every other proof here.

    A recovery kernel that enrols work structurally — being a registered model
    is what schedules it — gets this by construction; hand-written sweeps must
    be checked. The check is scheduler-neutral, because requiring one
    scheduler's configuration entry would turn a contract about recurring
    execution into a contract about that scheduler.
    """
    assert sweep.assert_scheduled is not None, (
        f"{sweep.name}: no schedule evidence supplied, so this proof cannot "
        f"verify that anything runs the tick. Pass `assert_scheduled=` — for "
        f"Celery beat, `due_work_harness.integrations.celery.celery_beat_evidence(<task path>)`"
    )
    sweep.assert_scheduled()


def assert_one_failing_row_does_not_stall_the_tick(
    *,
    name: str,
    make_owed: Callable[[], Any],
    run_tick: Callable[[], Any],
    fail_dispatch_for: Callable[[Any], Any],
    dispatched_ids: Callable[[], Any],
) -> None:
    """
    A single poisoned row must not block the rows behind it.

    A tick that dispatches in a loop and lets one exception escape abandons
    everything after it. Because selection is deterministic and oldest-first —
    invariants 6 and 7 — the *same* row leads every tick, so a permanently
    failing one blocks the queue forever rather than being overtaken. The
    ordering guarantees that make the sweep fair are exactly what make
    head-of-line blocking permanent.

    Standalone rather than part of :data:`DUE_WORK_PROOFS` because injecting a
    dispatch failure is adopter-specific; an adopter that cannot express it
    declares the rest of the profile without this one.
    """
    poison = make_owed()
    healthy = make_owed()

    fail_dispatch_for(poison)
    try:
        run_tick()
    except Exception as error:  # noqa: BLE001 - the failure under test
        raise AssertionError(
            f"{name}: one failing dispatch aborted the whole tick "
            f"({error!r}). Selection is deterministic and oldest-first, so the "
            f"same row leads every tick and everything behind it is blocked "
            f"permanently rather than overtaken"
        ) from error

    assert healthy in dispatched_ids(), (
        f"{name}: a row behind a failing one was not dispatched, so a single "
        f"poisoned row starves the rest of the backlog"
    )


def assert_outstanding_work_is_observable(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 9: an operator can see that work is piling up.

    The defects this kind of contract finds are overwhelmingly *silent*
    failures — rows stranded with no signal, items stuck in a processing state
    with no signal, sweeps scanning whole tables with no signal. None of them is
    hard to fix. All of them are hard to *notice*, and that is the property this
    invariant is about.

    Note what this is not. A counter that increments when recovery happens tells
    you loss already occurred; it cannot tell you that four hundred rows are
    stuck right now, because a sweep that never runs never increments it. The
    reading has to reflect the *backlog*, not the repair.

    An adopter that cannot supply such a reading fails this invariant, which is
    the point: without it, "the sweep silently stopped working" is
    indistinguishable from "there was no work to do".
    """
    assert sweep.observe_outstanding is not None, (
        f"{sweep.name}: no operator-visible reading of outstanding work. A stalled "
        f"sweep is then indistinguishable from an idle one, and the failures this "
        f"contract finds are silent rather than complicated"
    )

    for index in range(3):
        sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(minutes=index + 1))
    # Both endpoints, because a tick may legitimately change the backlog it is
    # reporting on. A stateless re-dispatch sweep leaves it untouched, so the two
    # agree; a sweep that claims or stamps the rows it dispatches shrinks it, and
    # then whether the published number matches depends only on whether the gauge
    # is read at the start of the tick or the end. That is an implementation
    # choice, not a property worth failing over — and comparing against the
    # post-tick count alone would report a violation against a correct claiming
    # sweep, which is the accusing direction this package treats as worse than
    # silence.
    before = len(_selected_identities(sweep))
    sweep.run_tick()
    after = len(_selected_identities(sweep))

    published = sweep.observe_outstanding()

    assert published is not None, (
        f"{sweep.name}: observe_outstanding() returned None after a tick, so the reading is never actually published"
    )
    # Compared against the real backlog rather than against a previous reading:
    # process-global metrics carry values from earlier tests in the same worker,
    # so "did it increase?" is not a question this can answer. "Does it match
    # reality?" is both answerable and the stronger property.
    assert published in (before, after), (
        f"{sweep.name}: the operator reading says {published} rows are "
        f"outstanding while the real backlog was {before} before the tick and "
        f"{after} after it. A reading that tracks neither cannot be alerted on"
    )


def assert_selection_does_not_read_the_replica(sweep: SelectionAdapter) -> None:
    """
    INVARIANT 10: due work is selected from the primary.

    A replica lags. Selecting due work from it re-dispatches effects that the
    primary already recorded as done, which for a non-idempotent provider means
    duplicating a real-world action. Applications usually make the primary the
    default and require replica routing to be deliberate, so this proof is a
    guard against someone "optimising" a hot scheduled query later.

    The host's inspector decides which database the selection reads, and fails
    rather than answer when the test setup makes the replica indistinguishable
    from the primary.
    """
    selection = sweep.due_work()
    replica = _inspector(selection, "assert_selection_does_not_read_the_replica").replica_read(selection)
    assert replica is None, (
        f"{sweep.name}: the due-work selection reads from {replica!r}. A lagging "
        f"replica reports work as outstanding that the primary already settled, "
        f"so the sweep re-dispatches completed effects"
    )


def assert_tick_cost_does_not_grow_with_the_backlog(
    *,
    name: str,
    make_owed: Callable[[], Any],
    run_tick: Callable[[], Any],
    selection: object | None = None,
) -> None:
    """
    A tick must not issue a query per row.

    A sweep that selects a page and then loads each row individually inside the
    loop is the classic N+1, and it is easy to introduce without noticing: a
    projection becomes a full-row iteration, or a dispatch argument starts
    reaching through a relation. Because the tick is bounded, the damage is
    capped per tick and therefore never shows up as an outage — it just makes
    the scheduled job quietly expensive forever.

    Compares statements for a small backlog against a larger one; equal counts
    mean the cost is in the selection rather than the loop. The host's inspector
    counts them; ``selection`` chooses the inspector when the host has several.
    """
    inspector = _statement_inspector("assert_tick_cost_does_not_grow_with_the_backlog", selection)
    for _ in range(2):
        make_owed()
    small_count = len(inspector.statements_during(run_tick))

    for _ in range(6):
        make_owed()
    large_count = len(inspector.statements_during(run_tick))

    assert large_count == small_count, (
        f"{name}: a tick over 8 rows issued {large_count} queries against "
        f"{small_count} for 2 rows, so cost grows per row rather than per tick. "
        f"The bound caps the damage, which is why this never surfaces as an "
        f"outage and stays expensive indefinitely"
    )


def assert_the_tick_restores_the_ambient_context(sweep: DueWorkSweep) -> None:
    """
    INVARIANT 11: a tick leaves the host's ambient context as it found it.

    Ambient per-call context — the current tenant, a request-scoped locale, a
    context variable that outlives the call — is written by code that pins it
    per row. A sweep that pins a tenant per row and does not restore it poisons
    whatever the worker process runs next — a different row, a different task,
    a different tenant — and the symptom appears far from the cause. This is a
    tenancy bug rather than a performance one, which is why it is worth a proof
    even where nothing currently violates it: every non-request caller should
    enter the context through a scope that restores it, and this is the guard
    that keeps it that way.

    Deliberately weaker than a per-row check. Verifying that each row is handled
    under its *own* context needs per-row observation the adopter must supply;
    verifying the tick does not leak needs nothing, and catches the same class
    of mistake at its most common boundary.

    Reads ``current_host().ambient_context`` and fails, naming the capability,
    when the host defines none — which is why it is in
    :data:`AMBIENT_CONTEXT_PROOFS` rather than :data:`DUE_WORK_PROOFS`.
    """
    ambient_context: Callable[[], object] = current_host().require("ambient_context")
    before = ambient_context()
    sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(minutes=1))
    sweep.run_tick()
    after = ambient_context()

    assert after == before, (
        f"{sweep.name}: the tick left the ambient context as {after!r} having "
        f"started at {before!r}. Whatever this worker process runs next inherits "
        f"it, so the damage lands somewhere unrelated to the cause. Enter the "
        f"context through a scope that restores it rather than setting it"
    )


def _scheduled_interval(sweep: DueWorkSweep) -> timedelta:
    """The sweep's declared tick interval."""
    assert sweep.tick_interval is not None, (
        f"{sweep.name}: no tick_interval declared, so no timing property can be "
        f"derived. Pass `tick_interval=` — for Celery beat, "
        f"`due_work_harness.integrations.celery.celery_beat_interval(<task path>)`"
    )
    return sweep.tick_interval


def assert_recovery_latency_is_bounded(sweep: DueWorkSweep, *, worst_case: timedelta) -> None:
    """
    Worst-case time from stranded to re-dispatched is within a stated bound.

    Derived, not timed. The bound is ``recovery delay + tick interval``: a row
    can become stranded moments after a tick, then must wait out its eligibility
    delay and the next tick. Both numbers come from real configuration — the
    delay from the sweep, the interval from the scheduler's configuration — so
    this cannot drift away from what production actually does.

    Deliberately arithmetic rather than a stopwatch. A wall-clock measurement in
    a test measures the test machine, and would be flaky in CI while telling
    nobody anything about production.
    """
    interval = _scheduled_interval(sweep)
    recovery_delay = declared_recovery_delay(sweep)
    bound = recovery_delay + interval
    assert bound <= worst_case, (
        f"{sweep.name}: worst-case recovery takes {bound} (recovery delay {recovery_delay} "
        f"plus a {interval} tick) against a stated bound of {worst_case}. A row "
        f"stranded just after a tick waits for both"
    )


def assert_the_backlog_can_drain(sweep: DueWorkSweep, *, peak_arrival_per_hour: float) -> None:
    """
    The sweep can outpace the work arriving, so a backlog is recoverable.

    ``page_size`` per tick is a throughput ceiling. If arrivals exceed it the
    backlog grows without bound and every other invariant here still passes —
    the sweep is working perfectly and losing ground, which is a failure mode
    that looks like health right up until it doesn't.

    Also the number that says how long an incident takes to clear: a sweep
    draining 1,200 rows an hour needs three days to work through 100,000
    stranded rows, and that is worth knowing before the incident rather than
    during it.
    """
    interval = _scheduled_interval(sweep)
    page_size = _page_size(sweep)
    drain_per_hour = page_size * (3600.0 / interval.total_seconds())
    assert drain_per_hour > peak_arrival_per_hour, (
        f"{sweep.name}: drains at most {drain_per_hour:.0f} rows/hour "
        f"({page_size} per {interval}) against a peak arrival of "
        f"{peak_arrival_per_hour:.0f}/hour. The backlog grows without bound "
        f"while every other invariant still passes"
    )


def assert_selection_scan_ratio_is_bounded(
    sweep: DueWorkSweep, *, rows: int = 200, max_scanned_per_returned: float = 5.0
) -> None:
    """
    The selection does not read the whole table to return a few rows.

    A stronger measure than the plan-shape check in
    :func:`assert_selection_is_index_served`, which asks whether an index prunes
    at all. This asks *how well*: executing the selection, the database reports
    rows actually returned and rows discarded by filters, and a selection that
    discards hundreds per hit has an index that is not selective enough even
    though it technically prunes.

    Real work rather than a timing: the ratio is a property of the data and the
    index, so it is stable across machines in a way wall-clock never is.
    """
    for index in range(rows):
        sweep.make_owed(age=declared_recovery_delay(sweep) + timedelta(minutes=index + 1))

    selection = sweep.due_work()
    returned, removed = _inspector(selection, "assert_selection_scan_ratio_is_bounded").scan_counts(selection)
    assert returned > 0, f"{sweep.name}: the selection returned no rows, so the scan ratio cannot be measured"
    ratio = removed / returned
    assert ratio <= max_scanned_per_returned, (
        f"{sweep.name}: the selection discarded {removed:.0f} rows to return "
        f"{returned:.0f} (ratio {ratio:.1f}, limit {max_scanned_per_returned}). "
        f"An index that prunes but is not selective still reads most of the "
        f"table, and the cost grows with it"
    )


# --- Waste -------------------------------------------------------------------
#
# Waste is work the system performs that cannot advance any obligation. It is
# distinct from a defect: nothing is lost and no invariant above is violated, so
# it never shows up as an incident. It shows up as a scheduled job that costs
# more every month, and as provider calls nobody asked for.
#
# Three shapes are measurable here. Wasted *polls* — a tick that finds nothing
# still pays for its queries. Wasted *dispatch* — re-sending work whose previous
# dispatch is still running, so two workers do the same thing. Wasted *reads* —
# the selection reading rows it will discard, which is the scan ratio proof.


def assert_idle_tick_is_cheap(
    *, name: str, run_tick: Callable[[], Any], max_queries: int = 2, selection: object | None = None
) -> None:
    """
    A tick with nothing to do costs a bounded, small number of queries.

    Most ticks are idle — that is the price of low recovery latency, and it is
    the right trade. But the cost is paid on a schedule forever, so it has to
    stay flat: a per-row query that also runs in the empty path, or an
    observability read that grows, turns a cheap poll into a standing tax on a
    hot table.

    The host's inspector counts the statements; ``selection`` chooses the
    inspector when the host has several.
    """
    captured = _statement_inspector("assert_idle_tick_is_cheap", selection).statements_during(run_tick)

    statements = "; ".join(sql[:100] for sql in captured)
    assert len(captured) <= max_queries, (
        f"{name}: an idle tick issued {len(captured)} queries (limit "
        f"{max_queries}). Most ticks are idle, so this is paid on every "
        f"interval forever: {statements}"
    )


def assert_in_flight_work_is_not_redispatched(
    *,
    name: str,
    make_owed: Callable[[], Any],
    run_tick: Callable[[], Any],
    dispatch_count_for: Callable[[Any], int],
    ticks: int = 3,
) -> None:
    """
    Consecutive ticks do not re-send work whose dispatch is still running.

    A sweep whose due-work predicate is a function of the row's *creation* has
    no way to know a dispatch is in flight: the row still matches, so every tick
    sends it again. Correctness can survive this — an idempotent worker
    discards the duplicate — but the cost does not, because each duplicate is a
    full execution, and a worker slower than the tick interval accumulates them.

    The two ways out are the same composition that decides any redispatch
    grace: take an exclusive claim, so the duplicate loses and stops; or record
    when a row was dispatched, so the selection can skip it. A grace measured
    from creation covers only the first dispatch.
    """
    owed = make_owed()
    for _ in range(ticks):
        run_tick()

    dispatches = dispatch_count_for(owed)
    assert dispatches == 1, (
        f"{name}: one stranded row was dispatched {dispatches} times over "
        f"{ticks} ticks. Each duplicate is a full execution of the effect, and a "
        f"worker slower than the tick interval accumulates them. Either claim "
        f"exclusively or record the dispatch — a grace measured from creation "
        f"only ever covers the first one"
    )


def assert_selection_is_index_served(sweep: SelectionAdapter) -> None:
    """
    INVARIANT 5: an index actually narrows the scheduled selection.

    The selection runs on a schedule forever, so an unpruned one is a periodic
    full scan of a production table — invisible in review, silent in
    production. The host's inspector captures the plan and decides; for
    PostgreSQL the verdict is
    :func:`due_work_harness.integrations.postgres_plans.assert_plan_is_index_served`,
    whose docstring records the plan shapes that look index-served and are not.
    """
    selection = sweep.due_work()
    served, evidence = _inspector(selection, "assert_selection_is_index_served").index_served(selection)
    assert served, f"{sweep.name}: {evidence}"


#: The selection-only subset: applicable to any scheduled selection, with or
#: without a full sweep around it. :class:`due_work_harness.contract.ScheduledSelection`
#: runs exactly these; profile A composes them into :data:`DUE_WORK_PROOFS` below.
SELECTION_PROOFS: tuple[Callable[[SelectionAdapter], None], ...] = (
    assert_the_adapter_does_not_author_the_selection,
    assert_selection_is_index_served,
    assert_selection_does_not_read_the_replica,
)


#: Every framework-free proof, in the order :func:`assert_due_work_recovery_contract`
#: runs them. The lifecycle-state proofs (2b, 2c) come from the Django
#: integration's ``TERMINAL_OBLIGATION_PROOFS``; the ambient-context proof from
#: :data:`AMBIENT_CONTEXT_PROOFS`.
DUE_WORK_PROOFS: tuple[Callable[[DueWorkSweep], None], ...] = (
    # First, because every proof below measures whatever `due_work` describes.
    assert_the_adapter_does_not_author_the_selection,
    assert_the_adapter_does_not_author_the_tick,
    assert_the_tick_selects_what_the_adapter_describes,
    assert_recovers_stranded_work,
    assert_all_owed_variants_are_recovered,
    assert_terminal_rows_are_never_selected,
    assert_in_flight_work_is_not_duplicated,
    assert_tick_is_bounded,
    assert_oldest_work_is_recovered_first,
    assert_selection_is_stable,
    assert_selection_is_index_served,
    assert_the_tick_is_actually_scheduled,
    assert_outstanding_work_is_observable,
    assert_selection_does_not_read_the_replica,
)


#: Proofs that need the host's ``ambient_context``. Add them when your host
#: defines one; they fail, naming the capability, on a host that does not.
AMBIENT_CONTEXT_PROOFS: tuple[Callable[[DueWorkSweep], None], ...] = (assert_the_tick_restores_the_ambient_context,)


def assert_due_work_recovery_contract(
    sweep: DueWorkSweep, *, extra_proofs: tuple[Callable[[DueWorkSweep], None], ...] = ()
) -> None:
    """
    Run the binding guards and every invariant against one adopter.

    ``extra_proofs`` appends proofs that need more than the core can supply —
    :data:`AMBIENT_CONTEXT_PROOFS`, or the Django integration's lifecycle-state
    proofs.

    Prefer calling the individual proofs when an adopter has a known,
    documented gap: xfail that one invariant with a reason rather than skipping
    the whole contract, so the remaining guarantees stay enforced.
    """
    assert_sweep_bindings_are_production_bound(sweep)
    for proof in (*DUE_WORK_PROOFS, *extra_proofs):
        proof(sweep)
