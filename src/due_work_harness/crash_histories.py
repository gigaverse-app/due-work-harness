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
Four families of histories must reach that same outcome:

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

The verdict is differential: the adopter supplies how to arrange the state, the
real transition and an observation, never the expected value. Positive
controls keep agreement honest: normal operation must change the observation,
the transition must commit, every interrupted run must actually be interrupted,
and when every history converges, recovery must have changed the observation in
at least one of them — otherwise an inert recovery, or an observation that
cannot see the handoff, would make everything agree with an equally unfinished
normal operation.

Delivery is the host's: a :class:`Delivery` says how published work reaches its
worker, how it is lost, and how production recovers. A PostgreSQL job queue has
no message separate from the database, so its delivery declares
``can_lose = False`` and the lost-notification history is not applicable.

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

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import Any, Protocol

import pytest

from due_work_harness.binding import INVOCATION_AUTHORING_OPERATIONS, assert_binding_reaches_production
from due_work_harness.host import current_host
from due_work_harness.models import MISSING, HarnessModel, MutableHarnessModel, with_positional
from due_work_harness.worker_death import CallbackFailed, WorkerDied


class ExternalCall(HarnessModel):
    """
    Where the transition crosses into an external system: ``getattr(owner, attribute)``.

    Name the provider client or fake method that performs the effect, on the
    object the transition actually calls — an instance, a class or a module.
    The harness wraps it only while the transition runs, calls the original,
    and kills the worker right after the chosen call returns.
    """

    owner: object
    attribute: str

    def __init__(self, owner: object = MISSING, attribute: str = MISSING, /, **data: Any) -> None:
        super().__init__(**with_positional(data, owner=owner, attribute=attribute))

    def __str__(self) -> str:
        owner = getattr(self.owner, "__name__", type(self.owner).__name__)
        return f"{owner}.{self.attribute}"


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
    callbacks: int = 0
    #: Whether the run was interrupted: a death, a failed callback, or its messages lost.
    interrupted: bool = False


class _Worker:
    """A transition's worker as the history sees it: commits from the host, calls from the seams."""

    def __init__(self, database: Any, callbacks: Any, crash_after_call: int | None) -> None:
        self._database = database
        self._callbacks = callbacks
        self.calls = 0
        self._crash_after_call = crash_after_call
        self._died_after_call = False

    @property
    def commits(self) -> int:
        return self._database.commits if self._database is not None else 0

    @property
    def callbacks(self) -> int:
        return self._callbacks.callbacks if self._callbacks is not None else 0

    @property
    def callback_failed(self) -> bool:
        return self._callbacks is not None and self._callbacks.failed

    @property
    def dead(self) -> bool:
        return self._died_after_call or (self._database is not None and self._database.dead)

    def called(self, seam: ExternalCall) -> None:
        self.calls += 1
        if self.calls == self._crash_after_call:
            reason = f"worker died right after external call {self.calls} ({seam})"
            self._died_after_call = True
            if self._database is not None:
                self._database.kill_now(reason)
            raise WorkerDied(reason)

    def refuse_if_dead(self) -> None:
        if self.dead:
            raise WorkerDied("the worker is dead; it calls nothing more")


@contextmanager
def _worker(
    history: HandoffHistory[Any, Any],
    crash_after: int | None,
    crash_after_call: int | None,
    fail_callback: int | None,
) -> Iterator[_Worker]:
    host = current_host()
    with ExitStack() as stack:
        database = stack.enter_context(host.worker_killer(crash_after)) if host.worker_killer is not None else None
        breaker = host.callback_breaker
        callbacks = stack.enter_context(breaker(fail_callback)) if breaker is not None else None
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        worker = _Worker(database, callbacks, crash_after_call)

        def seam(call: ExternalCall, original: Callable[..., Any]) -> Callable[..., Any]:
            def dying_after(*args: Any, **kwargs: Any) -> Any:
                worker.refuse_if_dead()
                result = original(*args, **kwargs)
                worker.called(call)
                return result

            return dying_after

        for call in history.external_calls:
            patch.setattr(call.owner, call.attribute, seam(call, getattr(call.owner, call.attribute)))
        yield worker


def _run(
    delivery: Delivery,
    history: HandoffHistory[Any, Any],
    *,
    label: str,
    lose: bool = False,
    crash_after: int | None = None,
    crash_after_call: int | None = None,
    fail_callback: int | None = None,
) -> HistoryRun:
    handle = history.arrange()
    before = history.observe(handle)
    with delivery.session() as session:
        with _worker(history, crash_after, crash_after_call, fail_callback) as worker:
            try:
                history.transition(handle)
            except WorkerDied:
                assert worker.dead, "WorkerDied escaped from something other than the simulated death"
            except CallbackFailed:
                # The failure reached the caller, as the framework re-raised it: a real request errors here.
                assert worker.callback_failed, "CallbackFailed escaped from something other than the failed callback"
            else:
                assert not worker.dead, (
                    f"handoff {history.name!r} kept running after its worker died: something caught WorkerDied "
                    f"(a BaseException) and carried on. A dead process runs nothing further, so a handoff made "
                    f"after the catch would converge falsely; let it propagate"
                )
            commits, calls, callbacks = worker.commits, worker.calls, worker.callbacks
            interrupted = worker.dead or worker.callback_failed
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
        callbacks=callbacks,
        interrupted=interrupted or lose,
    )


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
    assert counted.calls or not history.external_calls, (
        f"{delivery.name}: handoff {history.name!r} names external calls "
        f"({', '.join(map(str, history.external_calls))}) but the transition made none of them, so no death "
        f"after an external call was tried. Name the seam the transition actually calls"
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
    for i in range(1, counted.callbacks + 1):
        failed = _run(delivery, history, label=f"after-commit callback {i} failed", fail_callback=i)
        assert failed.interrupted, (
            f"{delivery.name}: handoff {history.name!r} ran {counted.callbacks} after-commit callback(s), but the "
            f"rerun never reached callback {i}. The transition registers callbacks nondeterministically, so its "
            f"histories are not reproducible"
        )
        runs.append(failed)
    return runs


def assert_histories_converge(name: str, runs: list[HistoryRun]) -> None:
    """
    The differential verdict: every interrupted history reaches normal operation's outcome.

    ``runs[0]`` is normal operation. Hosts whose deaths cannot be injected
    in-process — a separate executor, a real process — arrange the deaths
    themselves and hand the runs here (see :mod:`due_work_harness.process_histories`);
    the verdict and its positive controls stay with the harness.
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
    assert not divergent, (
        f"{name}: normal operation reaches {delivered.after!r}, but these histories reach something else: "
        f"{divergent!r}. Work was lost or repeated. A loss is work handed off only by a message, a commit hook "
        f"or code after a commit that a dead worker never runs: commit the handoff with the state that owes it, "
        f"or make that state selectable by recovery. A repeat is recovery redoing an external call whose effect "
        f"already exists: make the call idempotent, or record the attempt before it"
    )
    assert any(run.after != run.midway for run in interrupted), (
        f"{name}: positive control failed: every history converged, but recovery changed the observation in "
        f"none of them, so agreement proves nothing. Either recovery is inert — normal operation then never "
        f"finishes either — or the observation cannot see the work the transition hands off"
    )


def assert_crash_at_every_commit_converges(delivery: Delivery, history: HandoffHistory[Any, Any]) -> None:
    """Lost messages, and a death right after any commit or named external call, reach normal operation's outcome."""
    assert_histories_converge(f"{delivery.name}: handoff {history.name!r}", crash_histories(delivery, history))
