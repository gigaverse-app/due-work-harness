"""
Crash histories: lost messages and dead workers never change a handoff's outcome.

A *handoff* is a transition that commits work and passes work on: a producer
that records an order and publishes its notification, a failure classification
that owes a retry, a saga that checkpoints each step. The failure these
histories hunt is the handoff that exists only in a message, a commit hook, or
the next few lines of code: normal operation looks right, and a single lost
message or dead process strands the work forever — or makes recovery do it
twice.

The reference outcome is **normal operation**: the real transition runs with
its notifications delivered, then the bounded recovery production performs.
Seven families of histories must reach that same outcome:

* **Notifications lost** — the same transition, every message it published
  dropped, then recovery. This finds work that exists only as a message.
* **Death right after each commit** — the host's worker killer counts the
  transition's commits; for each commit k the transition reruns and the worker
  dies right after commit k, before after-commit callbacks run. From then on
  every statement on its connection raises, so ``finally`` blocks and cleanup
  cannot write what a dead process never would. This finds a handoff split
  across transactions or deferred to a commit hook, without the adopter naming
  the boundary.
* **Death right after each external call** — for every seam the history names
  in ``external_calls``, the worker dies the moment call j returns: the
  external system acted, the process did nothing more. This finds the
  at-least-once repeat no commit boundary shows — recovery sending a second
  notification because the first was never recorded.
* **Each after-commit callback failing** — when the host has a
  ``callback_breaker``, for each after-commit callback i the transition reruns
  and callback i raises instead of running, as a bug, a timeout or a provider
  error would, while the process lives on. This finds work handed off only to a
  callback, and work lost because an *earlier* callback failed: Django skips
  every later callback of the same commit.
* **The broker refusing each publication** — when the host has a
  ``publication_breaker``, for each message j the transition publishes, it
  reruns and publish j raises the broker client's own connection error, as a
  broker that is down would, while the process lives on. This finds what a
  failed publish takes down with it: work handed off only to that message, and
  everything the code would have done after the publish — the rest of the
  callback, the callbacks after it, the caller's response.
* **Each signal receiver failing** — when the host has a ``receiver_breaker``
  for the signals an adopter names, for each receiver i those signals run, the
  transition reruns and receiver i raises instead of running. This finds what a
  framework does with a failing hook: a worker that records a finished task's
  outcome and then lets a ``task_finished`` receiver's error rewrite it, or a
  handoff made by a receiver that an earlier one's failure skips.
* **The reply to each commit lost** — when the host has a ``reply_breaker``,
  for each commit k the transition reruns, commit k lands, and the client then
  raises its connection error, as a connection dropped between the server
  applying a write and the worker reading the answer would. The process lives
  on and believes the write may have failed. This finds error handling that
  treats a committed outcome as a failure: a task recorded as finished and then
  rewritten as failed, or retried and run a second time.

The verdict is differential: the adopter supplies how to arrange the state, the
real transition and an observation, never the expected value. Positive
controls keep agreement honest: normal operation must change the observation
and reach the same observation when run twice (or nothing could be compared with it),
the transition must commit, every interrupted run must actually be interrupted,
and when every history converges, recovery must have changed the observation in
at least one of them — otherwise an inert recovery, or an observation that
cannot see the handoff, would make everything agree with an equally unfinished
normal operation.

Delivery is the host's: a :class:`Delivery` says how published work reaches its
worker, how it is lost, and how production recovers. A PostgreSQL job queue has
no message separate from the database, so its delivery declares
``can_lose = False`` and the lost-notification history is not applicable.

What a history does with an exception that reaches it from the transition,
in this order (the same rule in every host, and in the Gigaverse backend's copy):

1. A seam's refusal (a deferred result it cannot observe) is raised, even when
   production swallowed or wrapped it.
2. After a simulated worker death, everything is absorbed, a failed assertion
   included: it was raised on the way out (a close, a ``finally``, a cleanup's
   check), which a dead process never runs. The death itself may arrive inside
   an exception group (a task group); a group holding ``WorkerDied`` is the
   death. ``WorkerDied`` without a simulated death is refused.
3. Otherwise a failed assertion, bare or anywhere inside an exception group,
   is raised: an invariant failing is a defect, whatever happened before it.
4. After an injected failure (a failed callback or receiver, a refused
   publication, a lost reply), an exception is absorbed when it is that
   failure, reaches it through ``__cause__`` (``raise … from failure``), is a
   deliberate translation (``raise … from None`` while handling it), or is a
   group every member of which is one of those: the application's own
   response, as a real request errors.
5. Anything else fails the history. An error linked to the injected failure
   only implicitly, through ``__context__`` (raised inside the handler
   without ``from``), is usually a bug in the handler; it and an error not
   linked at all get a note saying to chain it.

What these histories do not claim:

* The delivered outcome is whatever production does in normal operation;
  whether that is the right product behaviour is for behaviour goldens.
* Commits are counted where the host's worker killer counts them, typically the
  calling thread's default connection. Work committed on another connection is
  neither counted nor interrupted.
* Only the connection and the named seams die. Code that calls an unnamed
  external system after the death still runs, where a dead process's would not.
* Deaths happen inside the transition. A worker that recovery delivers is not
  killed after its own external calls; prove its replay safety with
  :class:`~due_work_harness.safety.replay_safe_execution.ReplaySafeEffect`, or
  run whole processes with :mod:`due_work_harness.process_histories`.
"""

import concurrent.futures
import traceback
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from inspect import getattr_static, isasyncgen, isawaitable, iscoroutine, iscoroutinefunction, isgenerator
from typing import Any, NamedTuple, Protocol

from due_work_harness.binding import INVOCATION_AUTHORING_OPERATIONS, assert_binding_reaches_production
from due_work_harness.host import CountedFaults, current_host
from due_work_harness.models import (
    MISSING,
    DueWorkContractDesignError,
    HarnessModel,
    MutableHarnessModel,
    with_positional,
)
from due_work_harness.recording import current_recorder
from due_work_harness.worker_death import WorkerDied


class ExternalCall(HarnessModel):
    """
    Where the transition crosses into an external system: ``getattr(owner, attribute)``.

    Name the provider client or fake method that performs the effect, on the
    object the transition actually calls — an instance, a class or a module.
    The harness wraps it only while the transition runs, calls the original,
    and kills the worker right after the chosen call completes. A coroutine
    result is awaited first — coroutine creation is not an effect — so an async
    client, or a sync method returning a coroutine, is killed after its effect,
    never before it. Any other deferred result (an async generator, a generator,
    a Task, a ``concurrent.futures.Future`` or an awaitable object) is refused:
    declare the coroutine method underneath it. On a class the seam keeps its ``staticmethod`` or
    ``classmethod`` binding, and is restored as it was. Declare each seam once.
    """

    owner: object
    attribute: str

    def __init__(self, owner: object = MISSING, attribute: str = MISSING, /, **data: Any) -> None:
        super().__init__(**with_positional(data, owner=owner, attribute=attribute))

    def __str__(self) -> str:
        owner = getattr(self.owner, "__name__", type(self.owner).__name__)
        return f"{owner}.{self.attribute}"


class Findings(HarnessModel):
    """
    What a history set leaves, declared with the history: normal operation's outcome, and each divergence.

    ``delivered`` is what normal operation reaches. ``outcomes`` maps the label
    of each history that reaches something else to what it reaches; every
    history not named must reach ``delivered``. Declared on a history, the
    table is checked in the same run as the convergence verdict, so the
    histories run once, and a finding that moves fails the case rather than
    passing as the known gap.
    """

    delivered: Any
    outcomes: dict[str, Any] = {}

    def __init__(self, delivered: Any = MISSING, outcomes: dict[str, Any] | None = None, /, **data: Any) -> None:
        super().__init__(
            **with_positional(data, delivered=delivered, outcomes=outcomes if outcomes is not None else MISSING)
        )


class HistoriesDiverged(AssertionError):
    """
    The differential verdict's failure: some history reached another outcome than normal operation.

    Raised for every divergence, whether or not the history declares its
    :class:`Findings`. A declared gap is a strict xfail for this exception only,
    so a binding that breaks, a positive control that fails, a table whose
    finding moved or a design error is reported as the failure it is (a plain
    ``AssertionError``) instead of passing as the known gap.
    """


# Reported under the name it is imported by, as pytest prints it on every divergence.
HistoriesDiverged.__module__ = "due_work_harness"


class HandoffHistory[HandleT, ObservationT](HarnessModel):
    """
    One production transition that commits work and hands work off.

    ``arrange`` builds the state just before the transition and returns a
    handle; it may write directly. ``transition`` is the real production
    command — the request handler, the reconcile step, the service method — and
    receives the handle. ``observe`` returns a comparable snapshot of the
    product and obligation state reachable from the handle: statuses,
    successor counts, what external systems saw. Leave out identifiers and
    timestamps that differ between runs. ``external_calls`` names the seams
    where the transition crosses into an external system.
    """

    name: str
    arrange: Callable[[], HandleT]
    transition: Callable[[HandleT], object]
    observe: Callable[[HandleT], ObservationT]
    external_calls: tuple[ExternalCall, ...] = ()
    #: Optional: what the histories leave, checked in the same run as the verdict.
    findings: Findings | None = None

    def model_post_init(self, _context: Any) -> None:
        for index, call in enumerate(self.external_calls):
            for other in self.external_calls[:index]:
                if _same_seam(call, other):
                    # Wrapped twice, every call would be counted twice and the deaths misplaced.
                    raise DueWorkContractDesignError(
                        f"handoff {self.name!r} declares the external call {call} twice (also as {other}); "
                        f"declare each seam once"
                    )


def _same_seam(first: ExternalCall, second: ExternalCall) -> bool:
    """
    Whether two declarations wrap the same function for the same callers.

    The same function (resolved statically, through inheritance) declared on
    owners that overlap: one owner, a class and a subclass of it, or a class and
    an instance of it. Two instances of one class are different seams.
    """
    if first.attribute != second.attribute:
        return False
    if _resolved(first) is not _resolved(second):
        return False
    a, b = first.owner, second.owner
    if a is b:
        return True
    if isinstance(a, type) and isinstance(b, type):
        return issubclass(a, b) or issubclass(b, a)
    if isinstance(a, type):
        return isinstance(b, a)
    if isinstance(b, type):
        return isinstance(a, b)
    return False


def _resolved(call: ExternalCall) -> object:
    """The function a seam's attribute names, without running a descriptor: a static or class method's own."""
    found = getattr_static(call.owner, call.attribute)
    return found.__func__ if isinstance(found, (staticmethod, classmethod)) else found


class DeliverySession(Protocol):
    """One history's delivery: installed before the transition, used after it."""

    @property
    def can_lose(self) -> bool:
        """Whether published work can be lost separately from the database."""
        ...

    def deliver(self) -> None:
        """Run what the transition published, as normal operation would."""
        ...

    def lose(self) -> None:
        """Drop everything the transition published."""
        ...

    def recover(self) -> None:
        """Run the bounded normal recovery production performs."""
        ...


class Delivery(Protocol):
    """How a host delivers handed-off work and recovers it."""

    @property
    def name(self) -> str: ...

    def session(self) -> AbstractContextManager[DeliverySession]: ...


class _CallableSession(MutableHarnessModel):
    deliver_published: Callable[[], object] | None
    lose_published: Callable[[], object] | None
    recover_work: Callable[[], object]

    @property
    def can_lose(self) -> bool:
        return self.lose_published is not None

    def deliver(self) -> None:
        if self.deliver_published is not None:
            self.deliver_published()

    def lose(self) -> None:
        assert self.lose_published is not None, "this delivery cannot lose published work"
        self.lose_published()

    def recover(self) -> None:
        self.recover_work()


class CallableDelivery(HarnessModel):
    """
    A delivery made of three production callables.

    ``recover`` is what production runs after any transition: the queue's
    workers, plus any periodic recovery the application schedules. ``deliver``
    runs published work immediately (omit it when ``recover`` already does, as a
    worker draining a job table does). ``lose`` drops published work; omit it
    when nothing is published apart from the database itself.
    """

    name: str
    recover: Callable[[], object]
    deliver: Callable[[], object] | None = None
    lose: Callable[[], object] | None = None

    @contextmanager
    def session(self) -> Iterator[DeliverySession]:
        yield _CallableSession(deliver_published=self.deliver, lose_published=self.lose, recover_work=self.recover)


class HistoryRun(HarnessModel):
    """One history's observations: before the transition, before recovery, and after it."""

    label: str
    before: Any
    #: After the transition and any loss or death, before delivery and recovery.
    midway: Any
    after: Any
    commits: int = 0
    calls: int = 0
    #: Positions in ``HandoffHistory.external_calls`` of the seams the run actually called.
    exercised: tuple[int, ...] = ()
    callbacks: int = 0
    publications: int = 0
    receivers: int = 0
    replies: int = 0
    #: Whether the run was interrupted: a death, a failed callback or receiver, a refused publication,
    #: a lost reply, or its messages lost.
    interrupted: bool = False


class _FaultFamily(NamedTuple):
    """One family of injected faults: which host breaker counts them, and how its histories are named."""

    #: The Host field holding the breaker.
    breaker: str
    #: The HistoryRun field that counts the family's occurrences in a run.
    counted: str
    #: The label of the history in which occurrence ``i`` fails.
    label: str
    #: What the transition did ``n`` times, for the reproducibility message.
    made: str
    #: One occurrence, as the message names it.
    occurrence: str
    #: What a nondeterministic transition does, for the message.
    varies: str


#: The families of injected faults, in the order their histories run. Each breaker counts its
#: occurrences during a transition and, when told to, makes one fail while the process lives on.
_FAULT_FAMILIES = (
    _FaultFamily(
        breaker="callback_breaker",
        counted="callbacks",
        label="after-commit callback {i} failed",
        made="ran {n} after-commit callback(s)",
        occurrence="callback",
        varies="registers callbacks",
    ),
    _FaultFamily(
        breaker="publication_breaker",
        counted="publications",
        label="the broker refused publication {i}",
        made="published {n} message(s)",
        occurrence="publication",
        varies="publishes",
    ),
    _FaultFamily(
        breaker="reply_breaker",
        counted="replies",
        label="the reply to commit {i} was lost",
        made="made {n} commit(s)",
        occurrence="commit",
        varies="commits",
    ),
    _FaultFamily(
        breaker="receiver_breaker",
        counted="receivers",
        label="signal receiver {i} failed",
        made="ran {n} signal receiver(s)",
        occurrence="receiver",
        varies="sends signals",
    ),
)


class _Worker:
    """A transition's worker as the history sees it: commits from the host, calls from the seams, counted faults."""

    def __init__(self, database: Any, faults: dict[str, CountedFaults | None], crash_after_call: int | None) -> None:
        self._database = database
        self._faults = faults
        self.calls = 0
        self.seams: list[ExternalCall] = []
        #: A seam's refusal, kept so a transition that swallows it cannot hide it.
        self.refused: DueWorkContractDesignError | None = None
        self._crash_after_call = crash_after_call
        self._died_after_call = False

    @property
    def commits(self) -> int:
        return self._database.commits if self._database is not None else 0

    def count(self, family: _FaultFamily) -> int:
        faults = self._faults[family.counted]
        return faults.count if faults is not None else 0

    @property
    def injected_failures(self) -> tuple[BaseException, ...]:
        """The ordinary exceptions this run injected, which may reach the caller as the application lets them."""
        return tuple(faults.failure for faults in self._faults.values() if faults is not None and faults.failure)

    @property
    def dead(self) -> bool:
        return self._died_after_call or (self._database is not None and self._database.dead)

    def called(self, seam: ExternalCall) -> None:
        self.calls += 1
        self.seams.append(seam)
        if self.calls == self._crash_after_call:
            reason = f"worker died right after external call {self.calls} ({seam})"
            self._died_after_call = True
            if self._database is not None:
                self._database.kill_now(reason)
            raise WorkerDied(reason)

    def refuse_if_dead(self) -> None:
        if self.dead:
            raise WorkerDied("the worker is dead; it calls nothing more")


#: Which family's occurrence to fail in a run: (the family, the 1-based occurrence).
type _Injection = tuple[_FaultFamily, int]


def _external_call_wrapper(call: ExternalCall, original: Callable[..., Any], worker: _Worker) -> Callable[..., Any]:
    """Observe completion at one boundary for both direct and deferred results."""

    async def after_await(result: Awaitable[Any]) -> Any:
        try:
            worker.refuse_if_dead()
            value = await result
        finally:
            # A coroutine created before death but awaited in cleanup must
            # neither execute its effect nor leak as an unawaited coroutine.
            if worker.dead and iscoroutine(result):
                result.close()
        worker.called(call)
        return value

    def dying_after(*args: Any, **kwargs: Any) -> Any:
        worker.refuse_if_dead()
        result = original(*args, **kwargs)
        if iscoroutine(result):
            return after_await(result)
        _refuse_a_deferred_result(call, result, worker)
        worker.called(call)
        return result

    async def dying_after_await(*args: Any, **kwargs: Any) -> Any:
        return await dying_after(*args, **kwargs)

    # Preserve coroutine-function introspection for callers such as async_to_sync,
    # but inspect every result: a sync SDK method may return an awaitable too.
    return dying_after_await if iscoroutinefunction(original) else dying_after


def _refuse_a_deferred_result(call: ExternalCall, result: object, worker: _Worker) -> None:
    """
    A result whose effect happens later, when the caller consumes it, cannot be observed honestly.

    Counting the call when it returns kills the worker before the effect (the
    false green awaiting a coroutine removes); waiting for the effect would hand
    the caller something other than what production returns: no ``async with``,
    no Task callbacks, no iteration. The coroutine method underneath is the seam.
    """
    if isasyncgen(result):
        kind = "an async generator"
    elif isgenerator(result):
        kind = "a generator"
    elif isinstance(result, concurrent.futures.Future):
        kind = "a concurrent.futures.Future"
    elif isawaitable(result):
        kind = "an awaitable that is not a coroutine (a Future, a Task or an awaitable object)"
    else:
        return
    worker.refused = DueWorkContractDesignError(
        f"the external call {call} returned {kind}, whose effect happens only when the caller consumes it. "
        f"The harness cannot wait for that without changing what the caller receives, and not waiting would "
        f"kill the worker before the effect. Declare the coroutine method underneath it, the one that "
        f"performs the effect, as the seam"
    )
    raise worker.refused


def _patched_seam(call: ExternalCall, worker: _Worker) -> object:
    """The replacement for one seam, keeping the binding Python gave the original."""
    original = getattr(call.owner, call.attribute)
    descriptor = getattr_static(call.owner, call.attribute)
    if isinstance(call.owner, type) and isinstance(descriptor, staticmethod):
        return staticmethod(_external_call_wrapper(call, original, worker))
    if isinstance(call.owner, type) and isinstance(descriptor, classmethod):
        # The unbound function receives the class as its first argument, as the classmethod passes it.
        return classmethod(_external_call_wrapper(call, descriptor.__func__, worker))
    return _external_call_wrapper(call, original, worker)


@contextmanager
def _worker(
    history: HandoffHistory[Any, Any],
    crash_after: int | None,
    crash_after_call: int | None,
    inject: _Injection | None,
) -> Iterator[_Worker]:
    host = current_host()
    with ExitStack() as stack:
        database = stack.enter_context(host.worker_killer(crash_after)) if host.worker_killer is not None else None
        faults: dict[str, CountedFaults | None] = {}
        for family in _FAULT_FAMILIES:
            breaker = getattr(host, family.breaker)
            fail_at = inject[1] if inject is not None and inject[0] is family else None
            faults[family.counted] = stack.enter_context(breaker(fail_at)) if breaker is not None else None
        worker = _Worker(database, faults, crash_after_call)
        for call in history.external_calls:
            stack.callback(_install(call.owner, call.attribute, _patched_seam(call, worker)))
        yield worker


def _install(owner: object, name: str, replacement: object) -> Callable[[], None]:
    """
    Put ``replacement`` at ``owner.name``, and return what restores exactly what was there.

    An attribute the owner defines itself is put back as it was, descriptor and
    all. One it only inherits, or an instance reads from its class, is deleted
    again: assigning the value read back would leave a bound method in the
    instance's own namespace, shadowing every later patch of its class.
    """
    try:
        namespace: Any = vars(owner)
    except TypeError:
        # An instance with __slots__: the seam is a slot's value, which is put back, never deleted.
        held = getattr(owner, name)
        setattr(owner, name, replacement)
        return lambda: setattr(owner, name, held)
    if name in namespace:
        original = namespace[name]
        setattr(owner, name, replacement)
        return lambda: setattr(owner, name, original)
    setattr(owner, name, replacement)
    return lambda: delattr(owner, name)


def _run(
    delivery: Delivery,
    history: HandoffHistory[Any, Any],
    *,
    label: str,
    lose: bool = False,
    crash_after: int | None = None,
    crash_after_call: int | None = None,
    inject: _Injection | None = None,
) -> HistoryRun:
    handle = history.arrange()
    before = history.observe(handle)
    with delivery.session() as session:
        with _worker(history, crash_after, crash_after_call, inject) as worker:
            try:
                history.transition(handle)
            # KeyboardInterrupt and SystemExit are the operator's, never the history's to judge.
            except (Exception, WorkerDied, BaseExceptionGroup) as error:
                if not _absorbed(error, worker):
                    raise
                _release(error)
            else:
                assert not worker.dead, (
                    f"handoff {history.name!r} kept running after its worker died: something caught WorkerDied "
                    f"(a BaseException) and carried on. A dead process runs nothing further, so a handoff made "
                    f"after the catch would converge falsely; let it propagate"
                )
            if worker.refused is not None:
                # Reported even when the transition swallowed it, before anything else is judged.
                raise worker.refused
            commits, calls = worker.commits, worker.calls
            exercised = tuple(
                index
                for index, declared in enumerate(history.external_calls)
                if any(seen is declared for seen in worker.seams)
            )
            counts = {family.counted: worker.count(family) for family in _FAULT_FAMILIES}
            interrupted = worker.dead or bool(worker.injected_failures)
        if lose:
            session.lose()
            midway = history.observe(handle)
        else:
            midway = history.observe(handle)
            session.deliver()
        session.recover()
    return HistoryRun(
        label=label,
        before=before,
        midway=midway,
        after=history.observe(handle),
        commits=commits,
        calls=calls,
        exercised=exercised,
        interrupted=interrupted or lose,
        **counts,
    )


def _absorbed(error: BaseException, worker: _Worker) -> bool:
    """
    Whether a history absorbs an exception from its transition: the absorption rule, in the module docstring.

    Returns False for an exception the caller must re-raise, and raises itself
    only where the report is something other than ``error`` (a seam's refusal,
    a death that was never simulated).
    """
    # 1. A seam's refusal is reported as itself, even when production wrapped it in its own error.
    if worker.refused is not None:
        if error is worker.refused:
            return False
        raise worker.refused from error
    leaves = list(_leaves(error))
    if any(not isinstance(leaf, (Exception, WorkerDied)) for leaf in leaves):
        return False  # an interrupt or exit inside a group is the operator's
    if any(isinstance(leaf, WorkerDied) for leaf in leaves):
        assert worker.dead, "WorkerDied escaped from something other than the simulated death"
    # 2. A dead process runs nothing on its way out: whatever a close, a finally or a check raised there, it
    # raised because the harness killed the worker under it.
    if worker.dead:
        return True
    # 3. An invariant failing, in production or in a proof, is never absorbed, bare or grouped.
    if any(isinstance(leaf, AssertionError) for leaf in leaves):
        return False
    failures = worker.injected_failures
    # 4. The injected failure reached the caller, as the framework and the application let it, or the
    # application answered it with its own error, chained on purpose: a real request errors here.
    if any(_caused_by(error, failure, frozenset()) for failure in failures):
        return True
    # 5. Anything else is a defect of its own; say how to make a deliberate response recognisable.
    for failure in failures:
        where = "while handling" if _handled_during(error, failure, frozenset()) else "after"
        error.add_note(
            f"due-work-harness: raised {where} the injected failure {failure!r}, but not chained from it, so "
            f"the history fails. If this is the application's response to that failure, raise it from the "
            f"failure ('raise ... from error'), or 'from None' to replace it on purpose"
        )
    return False


def _release(error: BaseException) -> None:
    """
    Drop what an absorbed exception's frames hold, as a dead or failed request's would be.

    The frames a traceback keeps hold their locals, and those can hold threads:
    ``async_to_sync`` runs its loop on a one-shot executor that a death's
    traceback keeps in a cycle, so its idle thread lingers until some later
    collection, and warns about sessions in unrelated tests. A collection after
    the fact is not enough, because one that ran while the death unwound (a
    close or a log allocating) promotes the cycle past the young generation.
    Clearing the finished frames breaks the cycle at its root whatever
    generation it reached; the frame still running (this history's) is skipped.
    """
    for linked in _chain(error, set()):
        traceback.clear_frames(linked.__traceback__)


def _chain(error: BaseException, seen: set[int]) -> Iterator[BaseException]:
    """An exception, its group members and everything chained to them, each once."""
    if id(error) in seen:
        return
    seen.add(id(error))
    yield error
    members = error.exceptions if isinstance(error, BaseExceptionGroup) else ()
    for linked in (error.__cause__, error.__context__, *members):
        if linked is not None:
            yield from _chain(linked, seen)


def _leaves(error: BaseException) -> Iterator[BaseException]:
    """The exceptions an exception group holds, at any depth; an ordinary exception is its own leaf."""
    if isinstance(error, BaseExceptionGroup):
        for member in error.exceptions:
            yield from _leaves(member)
    else:
        yield error


def _caused_by(error: BaseException, cause: BaseException, path: frozenset[int]) -> bool:
    """
    Whether ``error`` is ``cause``, was raised from it, deliberately translates it, or is a group of such errors.

    Explicit links only: ``__cause__`` (``raise … from``), and ``__context__``
    when ``from None`` suppressed it (a translation made while handling the
    failure). An exception's chain can cycle, so ``path`` holds the exceptions
    already on the way here.
    """
    if error is cause:
        return True
    if id(error) in path:
        return False
    path = path | {id(error)}
    if isinstance(error, BaseExceptionGroup) and all(_caused_by(m, cause, path) for m in error.exceptions):
        return True
    if error.__cause__ is not None:
        return _caused_by(error.__cause__, cause, path)
    return error.__suppress_context__ and error.__context__ is not None and _caused_by(error.__context__, cause, path)


def _handled_during(error: BaseException, cause: BaseException, path: frozenset[int]) -> bool:
    """Whether ``error`` was raised while ``cause`` was being handled: linked by ``__context__`` alone."""
    if error is cause:
        return True
    if id(error) in path:
        return False
    path = path | {id(error)}
    links = [error.__cause__, error.__context__]
    if isinstance(error, BaseExceptionGroup):
        links.extend(error.exceptions)
    return any(link is not None and _handled_during(link, cause, path) for link in links)


def assert_handoff_bindings_are_production_bound(delivery: Delivery, history: HandoffHistory[Any, Any]) -> None:
    """The transition is the real command, and a callable delivery's recovery is real recovery."""
    assert_binding_reaches_production(
        adopter=delivery.name,
        field=f"handoff[{history.name}].transition",
        binding=history.transition,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the production command that classifies work and hands it off",
    )
    if isinstance(delivery, CallableDelivery):
        assert_binding_reaches_production(
            adopter=delivery.name,
            field=f"handoff[{history.name}].recover",
            binding=delivery.recover,
            forbidden=INVOCATION_AUTHORING_OPERATIONS,
            production_shape="the workers and periodic recovery production runs after a transition",
        )


def crash_histories(delivery: Delivery, history: HandoffHistory[Any, Any]) -> list[HistoryRun]:
    """Run normal operation and every interrupted history; normal operation first."""
    assert_handoff_bindings_are_production_bound(delivery, history)
    # Required, so that "every commit" can never quietly mean "none".
    current_host().require("worker_killer")
    delivered = _run(delivery, history, label="normal operation")
    assert delivered.after != delivered.before, (
        f"{delivery.name}: handoff {history.name!r} — positive control failed: normal operation left the "
        f"observation unchanged ({delivered.before!r}), so no history could diverge"
    )
    assert_normal_operation_repeats(
        f"{delivery.name}: handoff {history.name!r}",
        delivered,
        _run(delivery, history, label="normal operation, again"),
    )
    runs = [delivered]
    with delivery.session() as probe:
        can_lose = probe.can_lose
    counted = delivered
    if can_lose:
        counted = _run(delivery, history, label="notifications lost", lose=True)
        runs.append(counted)
    assert counted.commits, (
        f"{delivery.name}: handoff {history.name!r} committed nothing the host's worker killer saw, so there is "
        f"no boundary to crash after. Bind a transition that commits on the connection the killer watches"
    )
    # An exercised seam cannot vouch for an unrelated, stale declaration.
    unused = [str(call) for index, call in enumerate(history.external_calls) if index not in counted.exercised]
    assert not unused, (
        f"{delivery.name}: handoff {history.name!r} names external calls the transition never called: "
        f"{', '.join(unused)}. No death after those calls was tried. Name the seams the transition actually calls"
    )
    for k in range(1, counted.commits + 1):
        crashed = _run(delivery, history, label=f"worker died after commit {k}", crash_after=k)
        assert crashed.interrupted, (
            f"{delivery.name}: handoff {history.name!r} committed {counted.commits} time(s), but the rerun never "
            f"reached commit {k}. The transition commits nondeterministically, so its histories are not reproducible"
        )
        runs.append(crashed)
    for j in range(1, counted.calls + 1):
        crashed = _run(delivery, history, label=f"worker died after external call {j}", crash_after_call=j)
        assert crashed.interrupted, (
            f"{delivery.name}: handoff {history.name!r} made {counted.calls} external call(s), but the rerun never "
            f"reached call {j}. The transition calls nondeterministically, so its histories are not reproducible"
        )
        runs.append(crashed)
    for family in _FAULT_FAMILIES:
        made = getattr(counted, family.counted)
        for i in range(1, made + 1):
            failed = _run(delivery, history, label=family.label.format(i=i), inject=(family, i))
            assert failed.interrupted, (
                f"{delivery.name}: handoff {history.name!r} {family.made.format(n=made)}, but the rerun never "
                f"reached {family.occurrence} {i}. The transition {family.varies} nondeterministically, so its "
                f"histories are not reproducible"
            )
            runs.append(failed)
    return runs


def assert_normal_operation_repeats(name: str, delivered: HistoryRun, again: HistoryRun) -> None:
    """
    A second clean run reaches the first one's outcome, before any interrupted history is compared with it.

    A divergence is evidence only if normal operation reproduces itself: an
    observation that differs between two clean runs (a per-run task id, a fresh
    row's key, a timestamp) would make every history "diverge", and a declared
    gap accept that. Refused as a plain assertion, never as a divergence.
    """
    assert again.after == delivered.after, (
        f"{name} — normal operation observed {delivered.after!r}, then {again.after!r} on a second clean run. "
        f"The observation is not deterministic, so no history can be compared with it. Leave out identifiers "
        f"and timestamps that differ between runs"
    )


def assert_histories_converge(name: str, runs: list[HistoryRun]) -> None:
    """
    The differential verdict: every interrupted history reaches normal operation's outcome.

    ``runs[0]`` is normal operation. Hosts whose deaths cannot be injected
    in-process — a separate executor, a real process — arrange the deaths
    themselves and hand the runs here (see :mod:`due_work_harness.process_histories`);
    the verdict and its positive controls stay with the harness.

    A divergence raises :class:`HistoriesDiverged`, always, so a gap's xfail can
    accept that alone; every other failure is a plain assertion.
    """
    delivered, interrupted = runs[0], runs[1:]
    assert delivered.after != delivered.before, (
        f"{name}: positive control failed: normal operation left the observation unchanged "
        f"({delivered.before!r}), so no history could diverge"
    )
    assert interrupted, f"{name}: no interrupted history ran, so nothing was compared"
    uninterrupted = [run.label for run in interrupted if not run.interrupted]
    assert not uninterrupted, f"{name}: these histories were never interrupted: {uninterrupted}"
    divergent = {run.label: run.after for run in interrupted if run.after != delivered.after}
    if divergent:
        raise HistoriesDiverged(
            f"{name}: normal operation reaches {delivered.after!r}, but these histories reach something else: "
            f"{divergent!r}. Work was lost or repeated. A loss is work handed off only by a message, a commit hook "
            f"or code after a commit that a dead worker never runs: commit the handoff with the state that owes "
            f"it, or make that state selectable by recovery. A repeat is recovery redoing an external call whose "
            f"effect already exists: make the call idempotent, or record the attempt before it"
        )
    assert any(run.after != run.midway for run in interrupted), (
        f"{name}: positive control failed: every history converged, but recovery changed the observation in "
        f"none of them, so agreement proves nothing. Either recovery is inert — normal operation then never "
        f"finishes either — or the observation cannot see the work the transition hands off"
    )


def assert_crash_at_every_commit_converges(delivery: Delivery, history: HandoffHistory[Any, Any]) -> None:
    """
    Lost messages, and a death right after any commit or named external call, reach normal operation's outcome.

    When the history declares its :class:`Findings`, the same runs are first
    held to that table: a finding that moved fails here, as an ordinary
    ``AssertionError``, before the verdict raises :class:`HistoriesDiverged`.
    """
    name = f"{delivery.name}: handoff {history.name!r}"
    runs = crash_histories(delivery, history)
    assert_findings_hold(name, runs, history.findings)
    assert_histories_converge(name, runs)


def findings_from(runs: list[HistoryRun]) -> Findings:
    """The table these runs would pin: what normal operation reaches, and each history that reaches something else."""
    delivered = runs[0].after
    return Findings(delivered, {run.label: run.after for run in runs[1:] if run.after != delivered})


def assert_findings_hold(name: str, runs: list[HistoryRun], findings: Findings | None) -> None:
    """
    The runs match a history's declared findings table; nothing to check when it declares none.

    While ``pytest --due-work-record-findings`` is recording, the runs are
    recorded instead of checked (see :mod:`due_work_harness.recording`).
    """
    recorder = current_recorder()
    if recorder is not None:
        recorder.record(name, findings_from(runs))
        return
    if findings is not None:
        assert_runs_match_table(name, runs, delivered=findings.delivered, outcomes=findings.outcomes)


def assert_pinned_outcomes(
    delivery: Delivery, history: HandoffHistory[Any, Any], *, delivered: Any, outcomes: dict[str, Any]
) -> list[HistoryRun]:
    """
    What every history of a handoff leaves, pinned: an adopter's findings table.

    Normal operation must reach ``delivered``. Each history named in
    ``outcomes`` must reach exactly its entry, keyed by label, and must run;
    every other history must reach ``delivered``, so a table lists only the
    findings however many commits the transition makes. A legacy gap declared
    as one strict xfail says the handoff diverges; this says precisely how,
    history by history, so a change upstream or in the harness shows which
    entry moved. Returns the runs for any further assertion.
    """
    runs = crash_histories(delivery, history)
    assert_runs_match_table(f"{delivery.name}: handoff {history.name!r}", runs, delivered=delivered, outcomes=outcomes)
    return runs


def assert_runs_match_table(name: str, runs: list[HistoryRun], *, delivered: Any, outcomes: dict[str, Any]) -> None:
    """
    The findings-table verdict over any runs, normal operation first.

    ``runs[0]`` must reach ``delivered``; each history named in ``outcomes``
    must run and reach exactly its entry; every other history must reach
    ``delivered``. :func:`assert_pinned_outcomes` applies it to crash
    histories, :func:`~due_work_harness.process_histories.assert_pinned_process_outcomes`
    to process histories.
    """
    assert runs[0].after == delivered, f"{name}: normal operation reaches {runs[0].after!r}, not {delivered!r}"
    actual = {run.label: run.after for run in runs[1:]}
    expected = {label: outcomes.get(label, delivered) for label in actual}
    moved = {label: actual[label] for label in actual if actual[label] != expected[label]}
    moved |= {label: "<not run>" for label in outcomes.keys() - actual.keys()}
    assert not moved, f"{name}: these histories no longer leave what the table pins: " + "; ".join(
        f"{label}: pinned {repr(outcomes[label]) if label in outcomes else f'normal operation ({delivered!r})'}, now {now!r}"
        for label, now in sorted(moved.items())
    )
