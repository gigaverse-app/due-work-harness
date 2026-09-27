"""
The due-work recovery proofs (profile A), pointed at an implementation we control.

Four layers, matching where this profile's own defects have historically lived:

* **The behavioural proofs** against a small in-memory sweep, conforming and
  broken. Each broken variant overrides one property and fails the proof that
  hunts it.
* **The adapter checks** — 0a's bytecode heuristic against the binding shapes it
  was calibrated on, 0b's differential comparison, and the 0c/0d delegation
  guards.
* **The host plumbing** — proofs that need a database fact (a plan, a replica, a
  statement count) ask the host's selection inspector, and fail naming the
  missing capability when it has none. An in-memory inspector drives them here;
  the Django inspector against PostgreSQL is exercised in
  ``tests/django/automatic_recovery``.
* **Schedule evidence, timing helpers and adoption sugar.**

The plan verdict behind invariant 5 is pinned against canned plans in
``test_plan_verdict.py``.
"""

import itertools
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from due_work_harness.helpers import contract_params, undeclared
from due_work_harness.host import Host, current_host, hosted, packages
from due_work_harness.integrations.celery import celery_beat_evidence, celery_beat_interval
from due_work_harness.profiles.automatic_recovery import (
    AMBIENT_CONTEXT_PROOFS,
    DUE_WORK_PROOFS,
    DueWorkSweep,
    InFlightExecution,
    OwedWorkVariant,
    assert_all_owed_variants_are_recovered,
    assert_idle_tick_is_cheap,
    assert_in_flight_work_is_not_duplicated,
    assert_in_flight_work_is_not_redispatched,
    assert_oldest_work_is_recovered_first,
    assert_one_failing_row_does_not_stall_the_tick,
    assert_outstanding_work_is_observable,
    assert_published_work_is_recoverable,
    assert_recovers_stranded_work,
    assert_recovery_latency_is_bounded,
    assert_selection_does_not_read_the_replica,
    assert_selection_is_index_served,
    assert_selection_is_stable,
    assert_selection_scan_ratio_is_bounded,
    assert_sweep_bindings_are_production_bound,
    assert_terminal_rows_are_never_selected,
    assert_the_adapter_does_not_author_the_selection,
    assert_the_backlog_can_drain,
    assert_the_tick_is_actually_scheduled,
    assert_the_tick_restores_the_ambient_context,
    assert_the_tick_selects_what_the_adapter_describes,
    assert_tick_cost_does_not_grow_with_the_backlog,
    assert_tick_is_bounded,
)

_GRACE = timedelta(minutes=15)
_PAGE_SIZE = 3

#: Ambient per-call context the reference tick must leave as it found it, the
#: way a worker pins the current tenant per row.
_TENANT: ContextVar[str] = ContextVar("tenant", default="primary")

#: A stand-in production callable. The adapter-guard tests declare the harness
#: package as the host's production code, so any function defined in it counts
#: as production; this one is side-effect free.
production_call = current_host


class _Selection:
    """
    The due-work selection the in-memory sweep returns.

    Any iterable satisfies the proofs; this one also has ``count()`` so the
    backlog-reading counterfeit below reads like the query-object shape it
    imitates, and is what the in-memory inspector recognises.
    """

    def __init__(self, rows: Iterable["_WorkRow"]) -> None:
        self._rows = list(rows)

    def __iter__(self) -> Iterator["_WorkRow"]:
        return iter(self._rows)

    def __getitem__(self, item: slice) -> list["_WorkRow"]:
        return self._rows[item]

    def __len__(self) -> int:
        return len(self._rows)

    def count(self) -> int:
        return len(self._rows)


@dataclass(frozen=True)
class _WorkRow:
    pk: int
    age: timedelta
    terminal: bool = False


class _InMemorySweep:
    """
    A conforming due-work sweep over a list.

    Oldest-first, terminal-immune, recovery-delay-respecting, bounded,
    deterministic, per-row failure-isolated, and one "statement" per tick.
    Broken variants override exactly one property.
    """

    def __init__(self, *, claim_on_dispatch: bool = True) -> None:
        self.rows: list[_WorkRow] = []
        self._ids = itertools.count(1)
        self.last_dispatched: list[int] = []
        self.dispatch_counts: dict[int, int] = {}
        self.poisoned: set[int] = set()
        self.claimed: set[int] = set()
        self.claim_on_dispatch = claim_on_dispatch
        #: What the in-memory "database" executed, for the statement proofs.
        self.statements: list[str] = []

    def make_owed(self, *, age: timedelta | None = None) -> _WorkRow:
        row = _WorkRow(next(self._ids), _GRACE + timedelta(minutes=1) if age is None else age)
        self.rows.append(row)
        return row

    def make_terminal(self, *, age: timedelta) -> list[_WorkRow]:
        row = _WorkRow(next(self._ids), age, terminal=True)
        self.rows.append(row)
        return [row]

    def due_work(self) -> _Selection:
        self.statements.append("SELECT due work")
        due = [row for row in self.rows if not row.terminal and row.age > _GRACE and row.pk not in self.claimed]
        return _Selection(sorted(due, key=lambda row: (-row.age.total_seconds(), row.pk)))

    def run_tick(self) -> int:
        self.last_dispatched = []
        for row in self.due_work()[:_PAGE_SIZE]:
            try:
                self._dispatch(row)
            except Exception:  # noqa: BLE001 - per-row isolation under test
                continue
            self.last_dispatched.append(row.pk)
        return len(self.last_dispatched)

    def _dispatch(self, row: _WorkRow) -> None:
        if row.pk in self.poisoned:
            raise RuntimeError("broker refused this message")
        self.dispatch_counts[row.pk] = self.dispatch_counts.get(row.pk, 0) + 1
        if self.claim_on_dispatch:
            self.claimed.add(row.pk)

    def binding(self, **overrides: Any) -> DueWorkSweep:
        members: dict[str, Any] = dict(
            name="in-memory reference sweep",
            due_work=self.due_work,
            run_tick=self.run_tick,
            make_owed=self.make_owed,
            make_terminal=self.make_terminal,
            recovery_delay=_GRACE,
            page_size=_PAGE_SIZE,
            # Stands in for schedule evidence; the evidence helpers are tested below.
            assert_scheduled=lambda: None,
            observe_outstanding=lambda: float(len(self.due_work())),
            dispatched_ids=lambda: list(self.last_dispatched),
            in_flight=InFlightExecution(
                make_owed=self.make_owed,
                start=self.hold_in_flight,
                execution_count_for=lambda row: self.dispatch_counts.get(row.pk, 0),
            ),
        )
        members.update(overrides)
        return DueWorkSweep(**members)

    @contextmanager
    def hold_in_flight(self, _row: _WorkRow, start_recovery_tick: Callable[[], int]) -> Iterator[None]:
        start_recovery_tick()
        yield


@dataclass
class _InMemoryInspector:
    """
    The database facts about an in-memory selection, as a host's inspector supplies them.

    Index service, the replica read and the scan counts are whatever the test
    says the database would report; statements are what the sweep logged.
    """

    sweep: _InMemorySweep
    served: tuple[bool, str] = (True, "an index narrows every scan")
    replica: str | None = None
    discarded_per_returned: float = 0.0

    def understands(self, selection: object) -> bool:
        return isinstance(selection, _Selection)

    def index_served(self, selection: object) -> tuple[bool, str]:
        return self.served

    def replica_read(self, selection: object) -> str | None:
        return self.replica

    def scan_counts(self, selection: object) -> tuple[float, float]:
        assert isinstance(selection, _Selection)
        returned = float(len(selection))
        return returned, returned * self.discarded_per_returned

    def statements_during(self, run: Callable[[], object]) -> list[str]:
        start = len(self.sweep.statements)
        run()
        return self.sweep.statements[start:]


@contextmanager
def _database(sweep: _InMemorySweep, **facts: Any) -> Iterator[_InMemoryInspector]:
    inspector = _InMemoryInspector(sweep, **facts)
    with hosted(Host(selection_inspectors=(inspector,), ambient_context=_TENANT.get)):
        yield inspector


@pytest.fixture
def production_host() -> Iterator[Host]:
    """A host whose production code is the harness package, so ``production_call`` counts."""
    with hosted(Host(production_packages=packages("due_work_harness"))) as host:
        yield host


# --- The behavioural proofs, both directions ----------------------------------


@pytest.mark.parametrize("proof", (*DUE_WORK_PROOFS, *AMBIENT_CONTEXT_PROOFS), ids=lambda proof: proof.__name__)
def test_the_conforming_sweep_passes_every_proof(proof: Callable[[DueWorkSweep], None]) -> None:
    sweep = _InMemorySweep()
    with _database(sweep):
        proof(sweep.binding())


def test_every_declared_owed_variant_is_mechanically_recovered() -> None:
    sweep = _InMemorySweep()

    def make_alternate(*, age: timedelta) -> _WorkRow:
        return sweep.make_owed(age=age)

    assert_all_owed_variants_are_recovered(
        sweep.binding(additional_owed_variants=(OwedWorkVariant(name="alternate-state", make=make_alternate),))
    )


@pytest.mark.parametrize("claim_on_dispatch", [True, False], ids=["claiming", "redispatching"])
def test_owed_variants_beyond_one_page_are_recovered_page_by_page(claim_on_dispatch: bool) -> None:
    # An exhaustive state partition can declare more variants than one page holds;
    # a sweep that leaves dispatched rows eligible must still reach every variant.
    sweep = _InMemorySweep(claim_on_dispatch=claim_on_dispatch)
    variants = tuple(OwedWorkVariant(name=f"state-{index}", make=sweep.make_owed) for index in range(_PAGE_SIZE * 2))
    assert_all_owed_variants_are_recovered(sweep.binding(additional_owed_variants=variants))


def test_a_tick_that_dispatches_nothing_fails_rediscoverability() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="dispatched nothing"):
        assert_recovers_stranded_work(sweep.binding(run_tick=lambda: 0))


def test_a_selection_that_misses_stranded_work_fails_rediscoverability() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="strand it permanently"):
        assert_recovers_stranded_work(sweep.binding(due_work=lambda: _Selection(())))


def test_a_selection_that_includes_terminal_rows_is_caught() -> None:
    sweep = _InMemorySweep()

    def selects_terminal() -> _Selection:
        due = [row for row in sweep.rows if row.age > _GRACE]
        return _Selection(sorted(due, key=lambda row: -row.age.total_seconds()))

    with pytest.raises(AssertionError, match="terminal rows"):
        assert_terminal_rows_are_never_selected(sweep.binding(due_work=selects_terminal))


def test_a_terminal_factory_that_builds_nothing_is_refused() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="pass vacuously"):
        assert_terminal_rows_are_never_selected(sweep.binding(make_terminal=lambda *, age: []))


def test_a_selection_that_ignores_real_in_flight_work_is_caught() -> None:
    sweep = _InMemorySweep()

    def ignores_claims() -> _Selection:
        return _Selection(row for row in sweep.rows if not row.terminal)

    def duplicate_tick() -> int:
        sweep.last_dispatched = []
        for row in ignores_claims()[:_PAGE_SIZE]:
            sweep._dispatch(row)  # noqa: SLF001 - deliberately broken tick
            sweep.last_dispatched.append(row.pk)
        return len(sweep.last_dispatched)

    with pytest.raises(AssertionError, match="executed 2 times while the first execution was still in flight"):
        assert_in_flight_work_is_not_duplicated(sweep.binding(due_work=ignores_claims, run_tick=duplicate_tick))


def test_an_unbounded_tick_is_caught() -> None:
    sweep = _InMemorySweep()

    def unbounded_tick() -> int:
        due = sweep.due_work()
        sweep.last_dispatched = [row.pk for row in due]
        return len(due)

    with pytest.raises(AssertionError, match="against a declared\\s+page size"):
        assert_tick_is_bounded(sweep.binding(run_tick=unbounded_tick))


def test_a_newest_first_selection_is_caught() -> None:
    sweep = _InMemorySweep()

    def newest_first() -> _Selection:
        due = [row for row in sweep.rows if not row.terminal and row.age > _GRACE]
        return _Selection(sorted(due, key=lambda row: row.age.total_seconds()))

    with pytest.raises(AssertionError, match="newer row is selected before"):
        assert_oldest_work_is_recovered_first(sweep.binding(due_work=newest_first))


def test_an_unstable_selection_is_caught() -> None:
    sweep = _InMemorySweep()
    flip = itertools.count()

    def unstable() -> _Selection:
        due = [row for row in sweep.rows if not row.terminal and row.age > _GRACE]
        ordered = sorted(due, key=lambda row: -row.age.total_seconds())
        return _Selection(reversed(ordered) if next(flip) % 2 else ordered)

    with pytest.raises(AssertionError, match="two evaluations"):
        assert_selection_is_stable(sweep.binding(due_work=unstable))


def test_a_tick_that_diverges_from_the_declared_selection_is_caught() -> None:
    """Invariant 0b in the failing direction, with the divergence located."""
    sweep = _InMemorySweep()

    def skips_the_lead_row() -> int:
        page = sweep.due_work()[1 : _PAGE_SIZE + 1]
        sweep.last_dispatched = [row.pk for row in page]
        return len(page)

    with pytest.raises(AssertionError, match="First disagreement at position 0"):
        assert_the_tick_selects_what_the_adapter_describes(sweep.binding(run_tick=skips_the_lead_row))


def test_invariant_0b_reads_the_declared_dispatch_identity() -> None:
    """
    A non-pk dispatch identity is compared as the adapter declares it.

    ``identity_of`` exists so a sweep whose worker is keyed on something other
    than the primary key — an external id, an attempt tuple — can still say
    what ``dispatched_ids`` emits. Reading ``row.pk`` regardless would fail an
    unwaivable proof over a divergence neither of the adapter's halves has.
    """
    sweep = _InMemorySweep()
    # The recorder is local rather than the reference's `last_dispatched`,
    # which records pk identities — the point here is a sweep whose dispatched
    # identity is not the primary key.
    recorded: list[str] = []

    def dispatch_identity(row: _WorkRow) -> str:
        return f"work-{row.pk}"

    def run_tick() -> int:
        page = sweep.due_work()[:_PAGE_SIZE]
        recorded[:] = [dispatch_identity(row) for row in page]
        return len(page)

    binding = sweep.binding(run_tick=run_tick, identity_of=dispatch_identity, dispatched_ids=lambda: list(recorded))
    assert_the_tick_selects_what_the_adapter_describes(binding)


def test_a_missing_dispatch_recorder_is_a_refusal_for_0b() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="no dispatched_ids supplied"):
        assert_the_tick_selects_what_the_adapter_describes(sweep.binding(dispatched_ids=None))


def test_a_poisoned_row_stalling_the_tick_is_caught_and_isolation_passes() -> None:
    conforming = _InMemorySweep()
    ages = itertools.count()

    def owed_oldest_first(sweep: _InMemorySweep) -> Callable[[], int]:
        return lambda: sweep.make_owed(age=_GRACE + timedelta(days=10 - next(ages))).pk

    assert_one_failing_row_does_not_stall_the_tick(
        name="per-row isolated sweep",
        make_owed=owed_oldest_first(conforming),
        run_tick=conforming.run_tick,
        fail_dispatch_for=conforming.poisoned.add,
        dispatched_ids=lambda: conforming.last_dispatched,
    )

    bare_loop = _InMemorySweep()

    def tick_without_isolation() -> int:
        bare_loop.last_dispatched = []
        for row in bare_loop.due_work()[:_PAGE_SIZE]:
            bare_loop._dispatch(row)  # noqa: SLF001 - the loop under test
            bare_loop.last_dispatched.append(row.pk)
        return len(bare_loop.last_dispatched)

    with pytest.raises(AssertionError, match="aborted the whole tick"):
        assert_one_failing_row_does_not_stall_the_tick(
            name="bare-loop sweep",
            make_owed=owed_oldest_first(bare_loop),
            run_tick=tick_without_isolation,
            fail_dispatch_for=bare_loop.poisoned.add,
            dispatched_ids=lambda: bare_loop.last_dispatched,
        )


def test_stateless_redispatch_is_caught_by_the_waste_proof() -> None:
    """
    A grace keyed on creation covers only the first dispatch — the sweep
    re-sends the same row on every tick, which is waste, not a correctness bug.
    """
    sweep = _InMemorySweep(claim_on_dispatch=False)
    with pytest.raises(AssertionError, match="dispatched 3 times over"):
        assert_in_flight_work_is_not_redispatched(
            name="stateless re-dispatch sweep",
            make_owed=lambda: sweep.make_owed().pk,
            run_tick=sweep.run_tick,
            dispatch_count_for=lambda pk: sweep.dispatch_counts.get(pk, 0),
        )


def test_a_claiming_sweep_passes_the_waste_proof() -> None:
    sweep = _InMemorySweep(claim_on_dispatch=False)
    claimed: set[int] = set()

    def claiming_tick() -> int:
        sweep.last_dispatched = []
        for row in sweep.due_work()[:_PAGE_SIZE]:
            if row.pk in claimed:
                continue
            claimed.add(row.pk)
            sweep._dispatch(row)  # noqa: SLF001 - the loop under test
            sweep.last_dispatched.append(row.pk)
        return len(sweep.last_dispatched)

    assert_in_flight_work_is_not_redispatched(
        name="claiming sweep",
        make_owed=lambda: sweep.make_owed().pk,
        run_tick=claiming_tick,
        dispatch_count_for=lambda pk: sweep.dispatch_counts.get(pk, 0),
    )


def test_a_missing_backlog_reading_fails_observability() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="no operator-visible reading"):
        assert_outstanding_work_is_observable(sweep.binding(observe_outstanding=None))


def test_a_reading_that_tracks_nothing_fails_observability() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="tracks neither"):
        assert_outstanding_work_is_observable(sweep.binding(observe_outstanding=lambda: 12345.0))


def test_a_reading_that_is_never_published_fails_observability() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="never actually published"):
        assert_outstanding_work_is_observable(sweep.binding(observe_outstanding=lambda: None))


# --- Invariant 11: the ambient context -------------------------------------------


def test_a_tick_that_leaks_the_ambient_context_is_caught() -> None:
    sweep = _InMemorySweep()

    def leaking_tick() -> int:
        # Pins the context for "its row" and never restores it.
        _TENANT.set("secondary")
        return 0

    token = _TENANT.set("primary")
    try:
        with _database(sweep), pytest.raises(AssertionError, match="left the ambient context as 'secondary'"):
            assert_the_tick_restores_the_ambient_context(sweep.binding(run_tick=leaking_tick))
    finally:
        _TENANT.reset(token)


def test_a_tick_that_restores_the_ambient_context_passes() -> None:
    sweep = _InMemorySweep()

    def scoped_tick() -> int:
        token = _TENANT.set("secondary")
        try:
            return sweep.run_tick()
        finally:
            _TENANT.reset(token)

    with _database(sweep):
        assert_the_tick_restores_the_ambient_context(sweep.binding(run_tick=scoped_tick))


def test_the_ambient_context_proof_names_the_capability_a_host_lacks() -> None:
    # Opt-in: not in DUE_WORK_PROOFS, and refused rather than passed on a host without one.
    assert assert_the_tick_restores_the_ambient_context not in DUE_WORK_PROOFS
    with hosted(Host()), pytest.raises(AssertionError, match="'ambient_context'"):
        assert_the_tick_restores_the_ambient_context(_InMemorySweep().binding())


# --- Invariant 3's production-started in-flight execution --------------------


def test_a_recovery_delay_without_real_in_flight_execution_evidence_is_refused() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="no production-started in-flight execution"):
        assert_in_flight_work_is_not_duplicated(sweep.binding(in_flight=None))


def test_in_flight_arrangement_must_be_recovery_eligible_before_start() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="not recovery-eligible before start"):
        assert_in_flight_work_is_not_duplicated(
            sweep.binding(
                in_flight=InFlightExecution(
                    make_owed=lambda: sweep.make_owed(age=timedelta(0)),
                    start=sweep.hold_in_flight,
                    execution_count_for=lambda row: sweep.dispatch_counts.get(row.pk, 0),
                )
            )
        )


def test_starting_a_worker_directly_cannot_substitute_for_starting_the_recovery_tick() -> None:
    """
    A worker claim can hide the queue gap: once the worker starts, selection is
    safe, but the scheduled tick could have published the same still-eligible
    row repeatedly before that worker began.
    """

    class WorkerBoundarySweep(_InMemorySweep):
        def __init__(self) -> None:
            super().__init__()
            self.worker_started: set[int] = set()

        def due_work(self) -> _Selection:
            return _Selection(row for row in super().due_work() if row.pk not in self.worker_started)

        @contextmanager
        def hold_worker_execution(self, row: _WorkRow, _start_recovery_tick: Callable[[], int]) -> Iterator[None]:
            self.worker_started.add(row.pk)
            self._dispatch(row)
            yield

    sweep = WorkerBoundarySweep()
    with pytest.raises(AssertionError, match="production recovery tick"):
        assert_in_flight_work_is_not_duplicated(
            sweep.binding(
                in_flight=InFlightExecution(
                    make_owed=sweep.make_owed,
                    start=sweep.hold_worker_execution,
                    execution_count_for=lambda row: sweep.dispatch_counts.get(row.pk, 0),
                )
            )
        )


def test_an_observer_that_claims_work_ran_before_start_is_refused() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="reported 1 execution.*before start"):
        assert_in_flight_work_is_not_duplicated(
            sweep.binding(
                in_flight=InFlightExecution(
                    make_owed=sweep.make_owed,
                    start=sweep.hold_in_flight,
                    execution_count_for=lambda _row: 1,
                )
            )
        )


def test_a_recovery_tick_with_no_observable_execution_is_refused() -> None:
    sweep = _InMemorySweep()

    @contextmanager
    def invisible_execution(_row: _WorkRow, start_recovery_tick: Callable[[], int]) -> Iterator[None]:
        start_recovery_tick()
        yield

    with pytest.raises(AssertionError, match="did not produce exactly one observable execution"):
        assert_in_flight_work_is_not_duplicated(
            sweep.binding(
                in_flight=InFlightExecution(
                    make_owed=sweep.make_owed,
                    start=invisible_execution,
                    execution_count_for=lambda _row: 0,
                )
            )
        )


def test_a_start_that_invokes_the_recovery_tick_twice_is_refused() -> None:
    sweep = _InMemorySweep()

    @contextmanager
    def starts_twice(_row: _WorkRow, start_recovery_tick: Callable[[], int]) -> Iterator[None]:
        start_recovery_tick()
        start_recovery_tick()
        yield

    with pytest.raises(AssertionError, match="invoked the production recovery tick 2 times"):
        assert_in_flight_work_is_not_duplicated(
            sweep.binding(
                in_flight=InFlightExecution(
                    make_owed=sweep.make_owed,
                    start=starts_twice,
                    execution_count_for=lambda row: sweep.dispatch_counts.get(row.pk, 0),
                )
            )
        )


def test_an_undeclared_recovery_delay_is_a_refusal_not_a_default() -> None:
    """``recovery_delay=None`` is absence; the proof must refuse, not assume zero."""
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="declared none"):
        assert_recovers_stranded_work(sweep.binding(recovery_delay=None))


def test_an_undeclared_page_size_is_a_refusal_not_a_default() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="declared no page size"):
        assert_tick_is_bounded(sweep.binding(page_size=None))


# --- Invariant 0a: the adapter must forward, not restate ----------------------


class _FakeManager:
    """
    Just enough object graph for a forwarding lambda to name attributes.

    Invariant 0a reads the binding's bytecode and never calls it, so these
    methods only need to exist, not to return anything query-shaped.
    """

    def due_for_x(self, cutoff: object) -> "_FakeManager":
        return self

    def filter(self, *args: object, **kwargs: object) -> "_FakeManager":
        return self

    def order_by(self, *fields: str) -> "_FakeManager":
        return self


class _FakeModel:
    objects = _FakeManager()


def _adapter_with(due_work: Callable[[], Any]) -> DueWorkSweep:
    refuse = undeclared("only invariant 0a is applied in this test")
    return DueWorkSweep(
        name="0a probe",
        due_work=due_work,
        run_tick=refuse,
        make_owed=refuse,
        make_terminal=refuse,
        recovery_delay=None,
        page_size=None,
    )


def test_a_forwarding_lambda_passes_0a() -> None:
    cutoff = object()
    assert_the_adapter_does_not_author_the_selection(_adapter_with(lambda: _FakeModel.objects.due_for_x(cutoff)))


def test_a_bare_bound_method_passes_0a() -> None:
    assert_the_adapter_does_not_author_the_selection(_adapter_with(_InMemorySweep().due_work))


def test_a_lambda_that_builds_the_query_fails_0a() -> None:
    with pytest.raises(AssertionError, match=r"authors production semantics in test code.*filter"):
        assert_the_adapter_does_not_author_the_selection(_adapter_with(lambda: _FakeModel.objects.filter(status="DUE")))


def test_a_query_built_behind_a_local_helper_fails_0a() -> None:
    """Moving the copy into a test-authored helper does not hide it: 0a follows the call."""

    def copied_selection() -> _FakeManager:
        return _FakeModel.objects.filter(status="DUE")

    with pytest.raises(AssertionError, match=r"authors production semantics in test code.*filter"):
        assert_the_adapter_does_not_author_the_selection(_adapter_with(lambda: copied_selection()))


def test_a_forwarding_lambda_that_adds_an_ordering_fails_0a() -> None:
    """
    Ordering is authorship: invariants 6 and 7 assert on the selection's
    order, so an adapter-applied order_by makes them measure fiction.
    """
    cutoff = object()
    with pytest.raises(AssertionError, match="order_by"):
        assert_the_adapter_does_not_author_the_selection(
            _adapter_with(lambda: _FakeModel.objects.due_for_x(cutoff).order_by("created_at"))
        )


def test_a_non_python_callable_fails_0a_rather_than_passing_silently() -> None:
    with pytest.raises(AssertionError, match="not an inspectable Python callable"):
        # A builtin is the wrong type on purpose: the guard must refuse what it cannot inspect.
        assert_the_adapter_does_not_author_the_selection(_adapter_with(min))  # pyrefly: ignore[bad-argument-type]


# --- The binding defenses, both directions -------------------------------------


def test_a_dispatched_ids_that_reads_the_selection_is_rejected() -> None:
    """
    Invariant 0b's counterfeit: a ``dispatched_ids`` derived from ``due_work``
    makes the differential proof compare the selection against itself, so the
    tick could dispatch anything (or nothing) and still "agree". A recorder is
    empty before the tick; a selection-reader already has rows.
    """
    sweep = _InMemorySweep()
    binding = sweep.binding(dispatched_ids=lambda: [row.pk for row in sweep.due_work()[:_PAGE_SIZE]])
    with pytest.raises(AssertionError, match="before any tick ran"):
        assert_the_tick_selects_what_the_adapter_describes(binding)


def test_a_test_authored_tick_is_rejected(production_host: Host) -> None:
    """
    A run_tick re-implemented in a test module can satisfy every behavioural
    proof — including 0b, whose "actual" side it feeds — while the production
    task never executes. It must reach production code.
    """
    sweep = _InMemorySweep()
    binding = sweep.binding(
        # The selection forwards (references production); only the tick is the
        # local re-implementation under test here.
        due_work=lambda: (production_call(), sweep.due_work())[1],
        in_flight=None,
    )
    with pytest.raises(AssertionError, match="run_tick.*references no production"):
        assert_sweep_bindings_are_production_bound(binding)


def test_a_test_authored_selection_is_rejected(production_host: Host) -> None:
    sweep = _InMemorySweep()
    binding = sweep.binding(run_tick=lambda: (production_call(), sweep.run_tick())[1], in_flight=None)
    with pytest.raises(AssertionError, match="due_work.*references no production"):
        assert_sweep_bindings_are_production_bound(binding)


def test_a_forwarding_tick_wrapper_is_accepted(production_host: Host) -> None:
    """A thin closure that calls a production callable is the sanctioned shape."""
    sweep = _InMemorySweep()
    binding = sweep.binding(
        due_work=lambda: (production_call(), sweep.due_work())[1],
        run_tick=lambda: (production_call(), sweep.run_tick())[1],
        # The reference sweep's default backlog reading counts its own
        # selection, which is exactly what invariant 0d refuses; the behavioural
        # proofs still use it, and this test is about the tick.
        observe_outstanding=None,
        in_flight=None,
    )
    assert_sweep_bindings_are_production_bound(binding)


def test_a_backlog_reading_computed_in_the_adapter_is_refused(production_host: Host) -> None:
    """
    Invariant 0d: counting the backlog here is not publishing it.

    This is the shape an invariant 9 gap invites — "a gauge of the backlog count
    published every tick" — with the publish left out. It matches the real
    backlog by construction, so invariant 9 passes forever while nothing outside
    the process can read anything.
    """
    sweep = _InMemorySweep()
    binding = sweep.binding(
        due_work=lambda: (production_call(), sweep.due_work())[1],
        run_tick=lambda: (production_call(), sweep.run_tick())[1],
        observe_outstanding=lambda: float(sweep.due_work().count()),
        in_flight=None,
    )
    with pytest.raises(AssertionError, match=r"observe_outstanding authors production semantics"):
        assert_sweep_bindings_are_production_bound(binding)


def test_a_backlog_reading_over_test_state_only_is_refused(production_host: Host) -> None:
    """Invariant 0d: a reading closed over test state reaches no production."""
    sweep = _InMemorySweep()
    binding = sweep.binding(
        due_work=lambda: (production_call(), sweep.due_work())[1],
        run_tick=lambda: (production_call(), sweep.run_tick())[1],
        observe_outstanding=lambda: float(len(sweep.rows)),
        in_flight=None,
    )
    with pytest.raises(AssertionError, match=r"observe_outstanding is test code that references no production"):
        assert_sweep_bindings_are_production_bound(binding)


def test_a_backlog_reading_that_forwards_to_production_is_accepted(production_host: Host) -> None:
    """0d accepts the shape it asks for: read a number production owns."""
    sweep = _InMemorySweep()
    binding = sweep.binding(
        due_work=lambda: (production_call(), sweep.due_work())[1],
        run_tick=lambda: (production_call(), sweep.run_tick())[1],
        observe_outstanding=lambda: (production_call(), float(len(sweep.due_work())))[1],
        in_flight=None,
    )
    assert_sweep_bindings_are_production_bound(binding)


def test_the_dispatch_recorder_is_deliberately_not_guarded_by_0d(production_host: Host) -> None:
    """
    0d does not reach ``dispatched_ids``, and that is a decision.

    It is documented as a recorder — a patched dispatch path appending what
    production sent — so a bare list is its intended shape and every adopter
    binds one. Its independence from the selection is already established by
    invariant 0b's ``premature == []`` check; a delegation tripwire here would
    reject correct adopters instead.
    """
    sweep = _InMemorySweep()
    binding = sweep.binding(
        due_work=lambda: (production_call(), sweep.due_work())[1],
        run_tick=lambda: (production_call(), sweep.run_tick())[1],
        observe_outstanding=None,
        dispatched_ids=lambda: list(sweep.last_dispatched),
        in_flight=None,
    )
    assert_sweep_bindings_are_production_bound(binding)


def test_a_covered_publishers_stranded_work_is_recovered() -> None:
    """The association ``covers=`` declares, run: publish, lose, recover."""
    sweep = _InMemorySweep()

    def publish_without_dispatching() -> int:
        # The publisher's own dispatch is the message that gets lost, so the
        # row is created and nothing is enqueued.
        return sweep.make_owed(age=timedelta(0)).pk

    def age_into_the_window(identity: int) -> None:
        sweep.rows = [
            row if row.pk != identity else _WorkRow(row.pk, _GRACE + timedelta(minutes=1)) for row in sweep.rows
        ]

    assert_published_work_is_recoverable(
        sweep.binding(),
        source_name="self-test publisher",
        publish=publish_without_dispatching,
        make_recovery_eligible=age_into_the_window,
    )


def test_a_publisher_whose_work_the_sweep_never_selects_is_caught() -> None:
    """
    The failure a static association could never see.

    The contract names a publisher and recovers a different class of row. Every
    other generated case stays green, because every other case arranges its own
    rows through ``make_owed``.
    """
    sweep = _InMemorySweep()

    def publishes_something_the_sweep_ignores() -> int:
        # Terminal rows are never selected, standing in for a publisher whose
        # work this sweep's predicate does not cover.
        return sweep.make_terminal(age=_GRACE + timedelta(hours=1))[0].pk

    with pytest.raises(AssertionError, match="the recovery selection never sees the row it leaves behind"):
        assert_published_work_is_recoverable(
            sweep.binding(),
            source_name="self-test publisher",
            publish=publishes_something_the_sweep_ignores,
            make_recovery_eligible=lambda _identity: None,
        )


def test_a_publisher_that_still_dispatches_cannot_prove_recovery() -> None:
    """
    The positive control: this proof is about a LOST dispatch.

    If the publisher's own message still fires, the work was never stranded,
    and "recovering" it would report a sound sweep whatever the selection does.
    """
    sweep = _InMemorySweep()

    def publish_and_dispatch() -> int:
        row = sweep.make_owed(age=_GRACE + timedelta(minutes=1))
        sweep.run_tick()
        return row.pk

    with pytest.raises(AssertionError, match="This proof is about a LOST dispatch"):
        assert_published_work_is_recoverable(
            sweep.binding(),
            source_name="self-test publisher",
            publish=publish_and_dispatch,
            make_recovery_eligible=lambda _identity: None,
        )


def test_an_owed_variant_the_selection_never_serves_fails() -> None:
    """A declared variant must actually be selected, or it is stranded work wearing a declaration."""
    sweep = _InMemorySweep()
    binding = sweep.binding(
        additional_owed_variants=(
            # Terminal rows are never selected, so a variant factory building
            # them names recovery the sweep does not perform.
            OwedWorkVariant(name="terminal-mislabeled-as-owed", make=lambda *, age: sweep.make_terminal(age=age)[0]),
        ),
    )
    with pytest.raises(AssertionError, match="terminal-mislabeled-as-owed"):
        assert_all_owed_variants_are_recovered(binding)


# --- The host plumbing: database facts come from the host's inspector ------------


def test_a_proof_needing_a_database_fact_names_the_missing_inspector() -> None:
    sweep = _InMemorySweep()
    with hosted(Host()):
        for proof in (assert_selection_is_index_served, assert_selection_does_not_read_the_replica):
            with pytest.raises(AssertionError, match="needs a SelectionInspector that understands _Selection"):
                proof(sweep.binding())


def test_an_unserved_selection_fails_with_the_inspectors_evidence() -> None:
    sweep = _InMemorySweep()
    with _database(sweep, served=(False, "sequential scan of work_rows")):
        with pytest.raises(AssertionError, match="in-memory reference sweep: sequential scan of work_rows"):
            assert_selection_is_index_served(sweep.binding())


def test_a_selection_routed_to_a_replica_is_caught() -> None:
    sweep = _InMemorySweep()
    with _database(sweep, replica="reader"), pytest.raises(AssertionError, match="reads from 'reader'"):
        assert_selection_does_not_read_the_replica(sweep.binding())


def test_the_scan_ratio_is_the_inspectors_discarded_over_returned() -> None:
    sweep = _InMemorySweep()
    with _database(sweep, discarded_per_returned=4.0):
        assert_selection_scan_ratio_is_bounded(sweep.binding(), rows=10)
    sweep = _InMemorySweep()
    with _database(sweep, discarded_per_returned=9.0), pytest.raises(AssertionError, match=r"ratio 9\.0, limit 5\.0"):
        assert_selection_scan_ratio_is_bounded(sweep.binding(), rows=10)


def test_an_empty_selection_cannot_measure_a_scan_ratio() -> None:
    sweep = _InMemorySweep()
    with _database(sweep), pytest.raises(AssertionError, match="returned no rows"):
        assert_selection_scan_ratio_is_bounded(sweep.binding(), rows=0)


def test_a_tick_loading_each_row_is_caught_and_a_constant_tick_passes() -> None:
    sweep = _InMemorySweep()
    with _database(sweep):
        assert_tick_cost_does_not_grow_with_the_backlog(
            name="one statement per tick", make_owed=sweep.make_owed, run_tick=sweep.run_tick
        )

    per_row = _InMemorySweep(claim_on_dispatch=False)

    def tick_loading_each_row() -> int:
        page = per_row.due_work()[:_PAGE_SIZE]
        per_row.statements.extend(f"SELECT row {row.pk}" for row in page)
        return len(page)

    with _database(per_row), pytest.raises(AssertionError, match="cost grows per row"):
        assert_tick_cost_does_not_grow_with_the_backlog(
            name="per-row loads", make_owed=per_row.make_owed, run_tick=tick_loading_each_row
        )


def test_an_idle_tick_is_cheap_and_a_chatty_one_is_caught() -> None:
    sweep = _InMemorySweep()
    with _database(sweep):
        assert_idle_tick_is_cheap(name="idle reference tick", run_tick=sweep.run_tick)

    def chatty_idle_tick() -> int:
        sweep.statements.extend(["SELECT due work", "SELECT metrics", "UPDATE heartbeat"])
        return 0

    with _database(sweep), pytest.raises(AssertionError, match="an idle tick issued 3 queries"):
        assert_idle_tick_is_cheap(name="chatty idle tick", run_tick=chatty_idle_tick)


def test_counting_statements_needs_exactly_one_inspector_or_a_named_selection() -> None:
    sweep = _InMemorySweep()
    with hosted(Host()), pytest.raises(AssertionError, match="the configured host has 0"):
        assert_idle_tick_is_cheap(name="no inspector", run_tick=sweep.run_tick)

    first, second = _InMemoryInspector(sweep), _InMemoryInspector(_InMemorySweep())
    with hosted(Host(selection_inspectors=(first, second))):
        with pytest.raises(AssertionError, match="Pass `selection=`"):
            assert_idle_tick_is_cheap(name="two inspectors", run_tick=sweep.run_tick)
        assert_idle_tick_is_cheap(name="named selection", run_tick=sweep.run_tick, selection=sweep.due_work())


# --- Schedule evidence and timing helpers --------------------------------------

#: A resolvable callable standing in for a scheduled task.
_SCHEDULED_TASK = "due_work_harness.references.in_memory.reference_apply_evidence"


def _beat_app(schedule: dict[str, dict[str, Any]]) -> SimpleNamespace:
    # The only surface the evidence helpers read from a Celery app.
    return SimpleNamespace(conf=SimpleNamespace(beat_schedule=schedule))


def test_beat_evidence_accepts_a_scheduled_resolvable_task() -> None:
    app = _beat_app({"entry": {"task": _SCHEDULED_TASK, "schedule": timedelta(hours=24)}})
    celery_beat_evidence(_SCHEDULED_TASK, app=app)()


def test_beat_evidence_rejects_an_unscheduled_task() -> None:
    with pytest.raises(AssertionError, match="not in the beat schedule"):
        celery_beat_evidence(_SCHEDULED_TASK, app=_beat_app({}))()


def test_beat_evidence_rejects_a_dangling_schedule_entry() -> None:
    """
    A beat entry pointing at a renamed task is exactly the 'nothing runs it'
    failure invariant 8 exists to catch, so the evidence must resolve the path.
    """
    dangling = "due_work_harness.references.in_memory.renamed_away_task"
    app = _beat_app({"entry": {"task": dangling, "schedule": timedelta(hours=24)}})
    with pytest.raises(AssertionError, match="does not resolve"):
        celery_beat_evidence(dangling, app=app)()


def test_beat_evidence_rejects_a_path_that_names_no_callable() -> None:
    constant = "due_work_harness.references.in_memory.REFERENCE_TERMINAL_STATES"
    app = _beat_app({"entry": {"task": constant, "schedule": timedelta(hours=24)}})
    with pytest.raises(AssertionError, match="neither callable nor a Celery task"):
        celery_beat_evidence(constant, app=app)()


def test_beat_interval_reads_the_schedule() -> None:
    app = _beat_app({"entry": {"task": _SCHEDULED_TASK, "schedule": timedelta(minutes=10)}})
    assert celery_beat_interval(_SCHEDULED_TASK, app=app) == timedelta(minutes=10)
    with pytest.raises(AssertionError, match="not in the beat schedule"):
        celery_beat_interval("pkg.unscheduled", app=app)


def test_a_schedule_that_is_not_an_interval_is_refused() -> None:
    app = _beat_app({"entry": {"task": _SCHEDULED_TASK, "schedule": "0 * * * *"}})
    with pytest.raises(AssertionError, match="not an interval"):
        celery_beat_interval(_SCHEDULED_TASK, app=app)


def test_missing_schedule_evidence_fails_invariant_8() -> None:
    sweep = _InMemorySweep()
    with pytest.raises(AssertionError, match="no schedule evidence"):
        assert_the_tick_is_actually_scheduled(sweep.binding(assert_scheduled=None))


def test_failing_schedule_evidence_fails_invariant_8() -> None:
    sweep = _InMemorySweep()
    evidence = celery_beat_evidence(_SCHEDULED_TASK, app=_beat_app({}))
    with pytest.raises(AssertionError, match="nothing runs it"):
        assert_the_tick_is_actually_scheduled(sweep.binding(assert_scheduled=evidence))


def test_recovery_latency_is_delay_plus_interval() -> None:
    sweep = _InMemorySweep()
    binding = sweep.binding(tick_interval=timedelta(minutes=10))
    assert_recovery_latency_is_bounded(binding, worst_case=timedelta(minutes=25))
    with pytest.raises(AssertionError, match="worst-case recovery"):
        assert_recovery_latency_is_bounded(binding, worst_case=timedelta(minutes=24))


def test_drain_rate_is_page_size_over_interval() -> None:
    sweep = _InMemorySweep()
    binding = sweep.binding(tick_interval=timedelta(minutes=10))
    # 3 rows per 10 minutes = 18/hour.
    assert_the_backlog_can_drain(binding, peak_arrival_per_hour=17)
    with pytest.raises(AssertionError, match="grows without bound"):
        assert_the_backlog_can_drain(binding, peak_arrival_per_hour=18)


def test_timing_proofs_refuse_an_undeclared_interval() -> None:
    binding = _InMemorySweep().binding()
    with pytest.raises(AssertionError, match="no tick_interval declared"):
        assert_recovery_latency_is_bounded(binding, worst_case=timedelta(hours=1))
    with pytest.raises(AssertionError, match="no tick_interval declared"):
        assert_the_backlog_can_drain(binding, peak_arrival_per_hour=1)


# --- Adoption sugar -------------------------------------------------------------


def test_undeclared_names_the_reason_when_reached() -> None:
    member = undeclared("this adopter has no per-row worker")
    with pytest.raises(NotImplementedError, match="no per-row worker"):
        member()


def test_contract_params_marks_exactly_the_named_gaps() -> None:
    gap_reason = "no index serves this selection yet"
    params = contract_params(DUE_WORK_PROOFS, gaps={"assert_selection_is_index_served": gap_reason})
    assert [param.id for param in params] == [proof.__name__ for proof in DUE_WORK_PROOFS]
    marked = {param.id: [mark for mark in param.marks if mark.name == "xfail"] for param in params}
    xfailed = {name for name, marks in marked.items() if marks}
    assert xfailed == {"assert_selection_is_index_served"}
    (mark,) = marked["assert_selection_is_index_served"]
    assert mark.kwargs == {"strict": True, "reason": gap_reason}


def test_contract_params_refuses_a_gap_that_names_no_proof() -> None:
    """A renamed proof must not silently orphan its xfail."""
    with pytest.raises(AssertionError, match="assert_selection_is_indexed_served"):
        contract_params(DUE_WORK_PROOFS, gaps={"assert_selection_is_indexed_served": "typo'd name"})
