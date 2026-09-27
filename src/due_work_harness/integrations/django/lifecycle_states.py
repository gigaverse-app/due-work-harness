"""
Terminal attempts must not hide live obligations (profile A, invariants 2b/2c).

A recovery sweep excludes terminal rows; invariant 2 proves it does. That
exclusion is only safe when "terminal" means **obligation-terminal**: nothing
the domain still owes hangs off the row. The motivating counterexample is a
recording pipeline whose service commits ``RETRYABLE_FAILED`` and creates the
successor attempt in a *separate*, later transaction, while the recovery
selection never returns ``RETRYABLE_FAILED``. A crash or lost message between
the two strands the retry, and a contract can stay green in two ways:

* the state is in neither ``make_terminal`` nor an owed variant, so no proof
  ever builds one (the "sampled terminal set" sham);
* labelling it terminal makes invariant 2 pass, because the selection correctly
  excludes it, while the obligation it hands off is still owed.

Two proofs close that gap. Both apply to every ``DueWorkSweep`` that declares a
:class:`Lifecycle`, and both need a Django ``QuerySet`` selection, because they
read the selection's WHERE clause and the model's ``choices``.

2b. **Every lifecycle state is declared** - the adopter names the production
    state field(s) in :class:`Lifecycle`. The harness reads their values from
    the model's own ``choices``, checks that the production selection actually
    filters on each field, builds every declared example, reads the persisted
    state of each, and requires every production value to be produced by an
    owed example, produced by a terminal example, or excluded with a reason -
    never a value the selection's own ``exact``/``in`` lookup selects. It covers
    each field's values, not their combinations or every admission shape. This
    is unwaivable: it establishes what the other proofs are about.
2c. **Terminal states owe nothing further** - each terminal example is
    delivered to the production per-identity execution (:attr:`Lifecycle.execute`).
    The delivery must create no new obligation: no row created (any ``INSERT``,
    ``MERGE`` or ``COPY`` as PostgreSQL reports it), no row of the model newly
    selectable by production recovery, no move of the delivered row's lifecycle
    state, no task published, no commit callback registered, no external call
    for the identity. Such a handoff exists only because the message arrived;
    recovery never selects the row, so a lost message loses it. Updates and
    deletes of rows that already exist are progress - a lease claim and release,
    an already-persisted successor advancing, an idempotent mirror. The positive
    control requires the same binding to change the owed row's state or create
    an obligation, before *and* after the terminal deliveries, so a no-op, a
    lease-only worker, or an ``execute`` that works only once cannot pass.

Host capabilities: ``frozen_clock`` (to age rows past the recovery delay) and,
unless the lifecycle declares ``publishes_nothing_because``,
``publication_recorder`` (to see work handed off only as a queue message).

What these proofs deliberately do not claim:

* A handoff that **no** recovery code performs - production drops the retry on
  the floor for a redelivered identity - is inert and passes 2c. The obligation
  is then invisible to every generic check; only an obligation inventory
  (state, still owed?, durable fact, selecting query, a test that loses its
  notification) exposes it.
* SQL is observed on the default connection of the calling thread. Writes from a
  thread-insensitive executor or another connection are outside the observer;
  publications and commit callbacks are observed process-wide.
* A write made by ``SELECT some_function(...)`` is classified by its command
  status, so it is not counted as a created row here; the newly-selectable and
  moved-state checks still apply.
* "Newly selectable" evaluates the production selection with the clock moved
  past the recovery delay. A selection that ages rows with SQL ``NOW()`` rather
  than the application clock sees no time pass, so a freshly reopened row stays
  invisible to this check (the ``INSERT`` still is not).
* ``deliver`` is ARRANGE code and is not guarded: a ``deliver`` that hands only
  terminal rows a stale envelope makes the worker refuse them. A reviewer checks
  that it builds what the tick's publication carries.
* ``excluded_states`` reasons are prose. Name the proof or production path that
  owns each excluded state; a reviewer checks the citation.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest import mock

from django.db import DEFAULT_DB_ALIAS, connection, connections
from django.db.models import Field, Lookup, Model, QuerySet
from django.db.models.expressions import Col
from django.db.models.sql.where import WhereNode
from django.utils import timezone

from due_work_harness.binding import (
    is_real_reason,
)
from due_work_harness.host import current_host
from due_work_harness.integrations.django.writes import row_write

if TYPE_CHECKING:
    from due_work_harness.profiles.automatic_recovery import DueWorkSweep

#: How far past the recovery delay the terminal examples are aged.
_SELECTION_HORIZON = timedelta(hours=1)

#: Age of the freshly transitioned terminal examples: seconds, the way a lost
#: message finds them, not zero, which a database clock behind the host would
#: still see in the future.
_FRESH_TERMINAL_AGE = timedelta(seconds=5)

#: Horizons past the recovery delay at which "newly selectable" is evaluated.
#: One horizon misses a successor unparked with a backoff longer than it; the
#: longest covers any backoff a product would tolerate for a handoff.
_SELECTABLE_HORIZONS = (_SELECTION_HORIZON, timedelta(days=1), timedelta(days=30))


@dataclass(frozen=True)
class Lifecycle:
    """
    The domain's lifecycle states and the execution that proves terminal ones inert.

    ``state_fields`` names the production model's lifecycle field(s) — the
    ``choices`` fields the selection filters on (``("status",)`` for recording
    attempts, ``("chat_status", "feed_status")`` for a two-sided membership
    sync). The harness reads the value set from the model, never from the
    adopter.

    ``execute`` is the production per-identity execution: the task body the
    tick publishes, the service method it calls, or the executor's synchronous
    admission path. It must read the identity it is handed. Replace only
    external providers at their seam; every application write and publication
    must stay real.

    ``inline_tick_because`` — for a tick that claims and executes inline, with
    no per-identity worker, bind the tick itself and say so here; 2c is then
    true by construction and the guide explains the limit.

    ``deliver`` is ARRANGE code, run before observation: it turns an example
    row into the argument ``execute`` receives, the way the tick's publication
    would — ``(room.id, room.name, room.sid)``, or a live dispatch reservation
    for a worker that refuses stale messages. A valid delivery is the strongest
    question to ask of a terminal row: would the worker act if a message for it
    did arrive? Defaults to ``identity_of(row)``.

    ``excluded_states`` maps a state no example produces to why this sweep need
    not cover it — the state precedes the obligation, or another named proof
    owns it. Key ``"field:value"`` for one field, or ``"value"`` for every state
    field whose choices include it. Silence is not one of the options.
    """

    state_fields: tuple[str, ...]
    execute: Callable[[Any], Any]
    excluded_states: Mapping[str, str] = field(default_factory=dict)
    deliver: Callable[[Any], Any] | None = None
    inline_tick_because: str | None = None

    #: OBSERVE — external effect calls made on behalf of this row's identity,
    #: counted at the provider fake (recordings started for the attempt,
    #: chat connections made for the member). A terminal delivery must not add one,
    #: and some owed delivery must, or the counter proves nothing.
    #: Per identity, not global: a predecessor delivery that advances its
    #: already-persisted successor calls the provider for the successor.
    effect_calls_for: Callable[[Any], int] | None = None

    #: Why the worker has no external effect to observe — it only reads, or its
    #: only effect is a publication 2c already sees. Exactly one of this and
    #: ``effect_calls_for`` is required: SQL and queue observation cannot see a
    #: second recording start or mail send.
    no_external_effect_because: str | None = None

    #: Why the worker never publishes to a queue outside the database, when the
    #: host has no ``publication_recorder``. Without one of them 2c cannot see a
    #: handoff made only as a message.
    publishes_nothing_because: str | None = None


def _lifecycle(sweep: "DueWorkSweep") -> Lifecycle:
    assert sweep.lifecycle is not None, (
        f"{sweep.name}: no `lifecycle=` declared. Name the production state field(s) and the "
        f"per-identity execution the tick dispatches, so the contract can prove every state is "
        f"accounted for and that terminal states owe nothing further — see Lifecycle"
    )
    return sweep.lifecycle


def _queryset(sweep: "DueWorkSweep") -> QuerySet[Any]:
    selection = sweep.due_work()
    assert isinstance(selection, QuerySet), (
        f"{sweep.name}: lifecycle proofs read the selection's WHERE clause, so due_work must return a Django "
        f"QuerySet, not {type(selection).__name__}"
    )
    return selection


def _ages(sweep: "DueWorkSweep") -> tuple[timedelta, timedelta]:
    """Owed examples age just past the recovery delay; terminal ones an hour past it."""
    from due_work_harness.profiles.automatic_recovery import declared_recovery_delay

    delay = declared_recovery_delay(sweep)
    return delay + timedelta(minutes=1), delay + _SELECTION_HORIZON


def _filtered_fields(where: WhereNode, model: type[Model]) -> set[str]:
    """Names of ``model`` fields the selection's WHERE clause compares."""
    names: set[str] = set()
    for child in where.children:
        if isinstance(child, WhereNode):
            names |= _filtered_fields(child, model)
        elif isinstance(child, Lookup) and isinstance(child.lhs, Col) and child.lhs.target.model is model:
            names.add(child.lhs.target.name)
    return names


def _is_state_lookup(child: object, model: type[Model], field_name: str) -> bool:
    return (
        isinstance(child, Lookup)
        and isinstance(child.lhs, Col)
        and child.lhs.target.model is model
        and child.lhs.target.name == field_name
        and child.lookup_name in ("exact", "in")
    )


def _constrains_only(where: WhereNode, model: type[Model], field_name: str) -> bool:
    """Every leaf under ``where`` is an ``exact``/``in`` lookup on ``field_name``."""
    return all(
        _constrains_only(child, model, field_name)
        if isinstance(child, WhereNode)
        else _is_state_lookup(child, model, field_name)
        for child in where.children
    )


def _admitted_values(where: WhereNode, model: type[Model], field_name: str, choices: set[str]) -> set[str] | None:
    """
    Values of ``field_name`` the predicate can admit, or ``None`` when it does not constrain them.

    ``exact``/``in`` lookups name values; AND intersects its constrained
    branches, OR unions them and is unconstrained if any branch is. A negated
    node — ``.exclude(status__in=...)``, ``~Q(...)`` — admits the complement
    over ``choices`` when every leaf beneath it compares only this field;
    a negation that mixes in other fields (``NOT (a AND b)`` is ``NOT a OR
    NOT b``) is unconstrained. The result is only used to refuse an
    exclusion, so uncertainty never accuses.
    """
    branches: list[set[str] | None] = []
    for child in where.children:
        if isinstance(child, WhereNode):
            branches.append(_admitted_values(child, model, field_name, choices))
        elif _is_state_lookup(child, model, field_name):
            assert isinstance(child, Lookup)
            values = child.rhs if child.lookup_name == "in" else (child.rhs,)
            branches.append(
                {str(value) for value in values} if isinstance(values, (list, tuple, set, frozenset)) else None
            )
        else:
            branches.append(None)
    admitted: set[str] | None
    if where.connector == "OR":
        known = [branch for branch in branches if branch is not None]
        admitted = set().union(*known) if len(known) == len(branches) else None
    else:
        constrained = [branch for branch in branches if branch is not None]
        admitted = set.intersection(*constrained) if constrained else None
    if not where.negated:
        return admitted
    if admitted is None or not _constrains_only(where, model, field_name):
        return None
    return choices - admitted


def _state_values(model: type[Model], state_field: str) -> set[str]:
    field_ = model._meta.get_field(state_field)
    assert isinstance(field_, Field), f"{model._meta.label}.{state_field} is a relation, not a lifecycle field"
    choices = field_.flatchoices
    assert choices, (
        f"{model._meta.label}.{state_field} declares no choices, so the harness cannot enumerate its "
        f"states from production. Name the field whose choices are the lifecycle"
    )
    return {str(value) for value, _label in choices}


def _exclusions(sweep: "DueWorkSweep", production_states: dict[str, set[str]]) -> dict[str, dict[str, str]]:
    """``excluded_states`` resolved to ``{field: {value: reason}}``, refusing unknown keys."""
    lifecycle = _lifecycle(sweep)
    resolved: dict[str, dict[str, str]] = {name: {} for name in production_states}
    unknown = []
    for key, reason in lifecycle.excluded_states.items():
        field_name, _, value = key.rpartition(":")
        targets = (
            [field_name] if field_name else [name for name, values in production_states.items() if value in values]
        )
        if not targets or any(value not in production_states.get(name, set()) for name in targets):
            unknown.append(key)
            continue
        for name in targets:
            resolved[name][value] = reason
    assert not unknown, f"{sweep.name}: excluded_states names values that are not production states: {sorted(unknown)}"
    return resolved


def _persisted_states(model: type[Model], rows: list[Any], fields: tuple[str, ...]) -> dict[str, set[str]]:
    """Read each example's state back from the database, never from the factory's object."""
    observed: dict[str, set[str]] = {name: set() for name in fields}
    for row in rows:
        assert isinstance(row, model), (
            f"an example returned {type(row).__name__}, not the selection's model {model.__name__}; "
            f"lifecycle proofs read each example's persisted state"
        )
        values = model._base_manager.filter(pk=row.pk).values(*fields).get()
        for name in fields:
            observed[name].add(str(values[name]))
    return observed


def assert_every_lifecycle_state_is_declared(sweep: "DueWorkSweep") -> None:
    """
    INVARIANT 2b: every production lifecycle state is owed, terminal, or excluded with a reason.

    One representative ``make_owed`` plus hand-picked terminal rows is a
    sample. A state absent from both — ``RETRYABLE_FAILED`` in the recording
    example — is never built, so no proof can say whether the sweep strands
    it. Enumerating the states from the model's ``choices`` turns that omission
    into a failing, named state.
    """
    lifecycle = _lifecycle(sweep)
    selection = _queryset(sweep)
    model = selection.model
    filtered = _filtered_fields(selection.query.where, model)
    production_states: dict[str, set[str]] = {}
    for name in lifecycle.state_fields:
        assert name in filtered, (
            f"{sweep.name}: the production selection does not filter on {model.__name__}.{name}, so it "
            f"is not the lifecycle this sweep recovers. Declare the field the selection compares"
        )
        production_states[name] = _state_values(model, name)

    exclusions = _exclusions(sweep, production_states)
    thin = sorted(
        f"{name}:{value}"
        for name, excluded in exclusions.items()
        for value, why in excluded.items()
        if not is_real_reason(why)
    )
    assert not thin, (
        f"{sweep.name}: excluded_states {thin} carry no real reason. Name the proof or production path "
        f"that owns each state, or why it precedes this obligation"
    )
    admitted = {
        name: _admitted_values(selection.query.where, model, name, production_states[name])
        for name in lifecycle.state_fields
    }
    selected_but_excluded = {
        name: sorted(set(excluded) & (admitted[name] or set()))
        for name, excluded in exclusions.items()
        if set(excluded) & (admitted[name] or set())
    }
    assert not selected_but_excluded, (
        f"{sweep.name}: excluded_states {selected_but_excluded} are values the production selection itself "
        f"selects. A state the sweep recovers owes work — declare it as an OwedWorkVariant, not an exclusion"
    )

    owed_age, terminal_age = _ages(sweep)
    owed = [sweep.make_owed(age=owed_age)]
    owed += [variant.make(age=owed_age) for variant in sweep.additional_owed_variants]
    terminal = sweep.make_terminal(age=terminal_age)
    covered = _persisted_states(model, owed + terminal, lifecycle.state_fields)

    contradictions = {
        name: sorted(covered[name] & set(excluded))
        for name, excluded in exclusions.items()
        if covered[name] & set(excluded)
    }
    assert not contradictions, (
        f"{sweep.name}: states {contradictions} are listed in excluded_states but an example produces "
        f"them. A state is either exercised or excluded, not both"
    )
    missing = {
        name: sorted(values - covered[name] - set(exclusions[name]))
        for name, values in production_states.items()
        if values - covered[name] - set(exclusions[name])
    }
    assert not missing, (
        f"{sweep.name}: production lifecycle states {missing} are produced by no example. For each: "
        f"if the selection owes work in it, add an OwedWorkVariant; if the selection excludes it, add it "
        f"to make_terminal, where invariant 2c then proves it owes nothing further; if it precedes this "
        f"obligation or another proof owns it, list it in Lifecycle.excluded_states with the reason"
    )


@dataclass
class _Effects:
    """
    What one delivery did, split into obligations and progress.

    Obligations are what a lost message would lose: a row that did not exist,
    a row of the model that production recovery newly selects, a move of the
    delivered row's lifecycle state, work published to a queue or deferred to
    a commit callback, or a recovery dispatch. Updates and deletes of existing
    rows are progress: a lease claim and release, an already-persisted
    successor advancing, an idempotent mirror.
    """

    created: list[str] = field(default_factory=list)
    newly_selectable: list[Any] = field(default_factory=list)
    moved_state: str | None = None
    published: list[str] = field(default_factory=list)
    commit_callbacks: int = 0
    dispatched: list[Any] = field(default_factory=list)
    effect_calls: int = 0
    progress: list[str] = field(default_factory=list)

    def _obligations(self) -> list[str]:
        """Each kind of new obligation this delivery left, described; empty when it owes nothing."""
        found = [
            (self.created, f"created rows {self.created}"),
            (self.newly_selectable, f"made rows newly selectable by recovery {self.newly_selectable}"),
            (self.moved_state is not None, f"moved the delivered row {self.moved_state}"),
            (self.published, f"published tasks {self.published}"),
            (self.commit_callbacks, f"{self.commit_callbacks} on_commit callback(s)"),
            (self.dispatched, f"recovery dispatches {self.dispatched}"),
            (self.effect_calls, f"{self.effect_calls} external effect call(s) for this identity"),
        ]
        return [description for present, description in found if present]

    @property
    def owes(self) -> bool:
        return bool(self._obligations())

    def describe(self) -> str:
        return "; ".join(self._obligations()) or "nothing"


def _new_entries(before: list[Any], after: list[Any]) -> list[Any]:
    """What a recorder gained: its new tail, or everything if it was reset in between."""
    return after[len(before) :] if after[: len(before)] == before else after


def _selectable(sweep: "DueWorkSweep") -> dict[timedelta, set[Any]]:
    """Identities the production selection returns at each horizon past the recovery delay."""
    from due_work_harness.profiles.automatic_recovery import declared_recovery_delay

    base = timezone.now() + declared_recovery_delay(sweep)
    selected: dict[timedelta, set[Any]] = {}
    for horizon in _SELECTABLE_HORIZONS:
        with current_host().require("frozen_clock")(base + horizon):
            selected[horizon] = {sweep.identity_of(row) for row in sweep.due_work()}
    return selected


def _newly_selectable(before: dict[timedelta, set[Any]], after: dict[timedelta, set[Any]]) -> list[Any]:
    return sorted(set().union(*(after[horizon] - before[horizon] for horizon in after)), key=repr)


@contextmanager
def _observed_effects(sweep: "DueWorkSweep") -> Iterator[_Effects]:
    """
    Record created rows, task publications and commit callbacks.

    Rows are classified by PostgreSQL's command status, so a CTE write, a
    commented statement or ``MERGE`` is seen; a guarded update that matches
    nothing is not a write. Publications are captured by the host's
    ``publication_recorder``, held and never delivered; the adopter's own
    dispatch recorder is compared before and after as well, for adapters that
    patch a task's ``delay`` on the instance.
    """
    effects = _Effects()
    dispatched_before = list(sweep.dispatched_ids()) if sweep.dispatched_ids is not None else []
    wrapper_type = type(connections[DEFAULT_DB_ALIAS])
    register_callback = wrapper_type.on_commit

    def record_write(execute: Callable[..., Any], sql: str, params: Any, many: bool, context: dict[str, Any]) -> Any:
        result = execute(sql, params, many, context)
        write = row_write(sql, context["cursor"])
        if write is not None:
            (effects.created if write.creates_rows else effects.progress).append(str(write))
        return result

    def record_callback(wrapper: Any, func: Callable[[], Any], *args: Any, **kwargs: Any) -> Any:
        effects.commit_callbacks += 1
        return register_callback(wrapper, func, *args, **kwargs)

    recorder = current_host().publication_recorder
    with (
        connection.execute_wrapper(record_write),
        mock.patch.object(wrapper_type, "on_commit", record_callback),
        recorder() if recorder is not None else nullcontext([]) as published,
    ):
        yield effects
    effects.published = list(published)
    if sweep.dispatched_ids is not None:
        effects.dispatched = _new_entries(dispatched_before, list(sweep.dispatched_ids()))


def _execute_observed(sweep: "DueWorkSweep", lifecycle: Lifecycle, row: Any) -> tuple[_Effects, Exception | None]:
    """Deliver one row to the production execution; an exception is evidence, not an escape."""
    failure: Exception | None = None
    model = type(row)
    delivery = lifecycle.deliver(row) if lifecycle.deliver is not None else sweep.identity_of(row)
    state_before = _persisted_states(model, [row], lifecycle.state_fields)
    selectable_before = _selectable(sweep)
    calls_before = lifecycle.effect_calls_for(row) if lifecycle.effect_calls_for is not None else 0
    with _observed_effects(sweep) as effects:
        try:
            lifecycle.execute(delivery)
        except Exception as error:  # noqa: BLE001 - reported with the effects it left behind
            failure = error
    if lifecycle.effect_calls_for is not None:
        effects.effect_calls = lifecycle.effect_calls_for(row) - calls_before
    effects.newly_selectable = _newly_selectable(selectable_before, _selectable(sweep))
    if model._base_manager.filter(pk=row.pk).exists():
        state_after = _persisted_states(model, [row], lifecycle.state_fields)
        if state_after != state_before:
            effects.moved_state = f"from {state_before} to {state_after}"
    return effects, failure


def _require_effect_observation(owner: str, lifecycle: Lifecycle) -> None:
    observed = lifecycle.effect_calls_for is not None
    reason = (lifecycle.no_external_effect_because or "").strip()
    assert observed != bool(reason), (
        f"{owner}: declare exactly one of Lifecycle.effect_calls_for (count external effect calls per identity "
        f"at the provider fake) or Lifecycle.no_external_effect_because. A terminal delivery that only calls a "
        f"provider SDK writes no row and publishes no task, so without one of them 2c cannot see it"
    )
    assert observed or is_real_reason(reason), (
        f"{owner}: no_external_effect_because carries no real reason; name what the worker does instead"
    )


def _require_publication_observation(owner: str, lifecycle: Lifecycle) -> None:
    if current_host().publication_recorder is not None:
        return
    assert is_real_reason(lifecycle.publishes_nothing_because or ""), (
        f"{owner}: the host has no publication_recorder, so 2c cannot see work handed off only as a queue "
        f"message. Configure one (for example due_work_harness.integrations.celery.celery_publications) or "
        f"declare Lifecycle.publishes_nothing_because with the reason the worker never publishes"
    )


def _assert_effect_counter_counts(sweep: "DueWorkSweep", lifecycle: Lifecycle, owed_age: timedelta) -> None:
    """
    The positive control for ``effect_calls_for``: some owed delivery must raise the count.

    A counter that never counts (``lambda row: 0``) would vouch for every
    terminal delivery. The representative is tried first, then each owed
    variant; a variant that raises after reaching the provider still counts,
    because the call was made.
    """
    if lifecycle.effect_calls_for is None:
        return
    for make in (sweep.make_owed, *(variant.make for variant in sweep.additional_owed_variants)):
        effects, _failure = _execute_observed(sweep, lifecycle, make(age=owed_age))
        if effects.effect_calls:
            return
    raise AssertionError(
        f"{sweep.name}: effect_calls_for never counted a call — no owed delivery (the representative or any "
        f"OwedWorkVariant) raised it. A counter that cannot see owed work's provider calls cannot vouch that "
        f"terminal deliveries make none; count at the fake the worker actually calls"
    )


def _positive_control(sweep: "DueWorkSweep", lifecycle: Lifecycle, owed_age: timedelta, when: str) -> None:
    owed = sweep.make_owed(age=owed_age)
    control, failure = _execute_observed(sweep, lifecycle, owed)
    if failure is not None:
        failure.add_note(
            f"{sweep.name}: positive control ({when}) — lifecycle.execute raised on the owed row; an error on the "
            f"way to the work is not evidence that it ran"
        )
        raise failure
    assert control.owes, (
        f"{sweep.name}: positive control ({when}) failed — lifecycle.execute on an owed row "
        f"({sweep.identity_of(owed)!r}) neither changed its state nor created, published or selected anything"
        + (f" (progress only: {control.progress})" if control.progress else "")
        + ". Bind the per-identity worker the tick dispatches; a binding that never reaches the work makes "
        "every terminal verdict vacuous"
    )


def assert_terminal_states_owe_nothing_further(sweep: "DueWorkSweep") -> None:
    """
    INVARIANT 2c: delivering a terminal row to the production worker creates no new obligation.

    Invariant 2 proves the selection skips terminal rows. That is only safe if
    a message for the row can be lost without losing work. Delivering each
    terminal identity to the same per-identity execution the tick dispatches
    asks production directly: if it creates a row, makes a row newly
    selectable, moves the delivered row's state, publishes a task, registers
    a commit callback, or calls a provider for this identity, the state still carries a handoff — a successor, a
    fan-out, a follow-up — that exists only because the message arrived.

    Updates and deletes of existing rows are progress, not obligations: a
    lease claim and release, an already-persisted successor
    advancing, an idempotent product mirror.

    The positive control runs before and after the terminal deliveries: the
    same binding must change a fresh owed row's state or create an obligation
    both times, so an ``execute`` that reaches the work only once cannot make
    the terminal deliveries vacuous.
    """
    lifecycle = _lifecycle(sweep)
    _require_effect_observation(sweep.name, lifecycle)
    _require_publication_observation(sweep.name, lifecycle)
    owed_age, terminal_age = _ages(sweep)
    _positive_control(sweep, lifecycle, owed_age, "before the terminal deliveries")
    _assert_effect_counter_counts(sweep, lifecycle, owed_age)

    # A terminal row's message is lost seconds after the transition, not an hour
    # later: a worker that hands off only while the failure is fresh must be
    # asked with a fresh row too.
    terminal = [*sweep.make_terminal(age=_FRESH_TERMINAL_AGE), *sweep.make_terminal(age=terminal_age)]
    assert terminal, f"{sweep.name}: make_terminal produced no rows, so this proof would pass vacuously"
    offenders = []
    for row in terminal:
        state = _persisted_states(type(row), [row], lifecycle.state_fields)
        effects, failure = _execute_observed(sweep, lifecycle, row)
        if effects.owes:
            offenders.append(
                f"{sweep.identity_of(row)!r} in {state}: {effects.describe()}"
                + (f" (then raised {failure!r})" if failure else "")
            )
        elif failure is not None:
            failure.add_note(f"{sweep.name}: lifecycle.execute raised on terminal example {state}")
            raise failure
    assert not offenders, (
        f"{sweep.name}: terminal-labelled rows still owe work — delivering them to the production "
        f"execution created new obligations: {offenders}. The selection never redelivers a terminal "
        f"row, so if that message is lost the handoff never happens. Either commit the handoff in the "
        f"same transaction as the state, or make the state selectable (an OwedWorkVariant) so recovery "
        f"performs it"
    )
    _positive_control(sweep, lifecycle, owed_age, "after the terminal deliveries")


#: Generated for every ``DueWorkSweep`` after invariant 2.
TERMINAL_OBLIGATION_PROOFS: tuple[Callable[["DueWorkSweep"], None], ...] = (
    assert_every_lifecycle_state_is_declared,
    assert_terminal_states_owe_nothing_further,
)
