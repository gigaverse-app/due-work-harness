"""
Celery worker integration: the application's real worker in a child process, failing at Celery's own stages.

A Celery worker runs each task in a pool child: the tracer runs the body,
publishes the task's links, stores its result, runs its hooks, and the parent
acknowledges the message. A test cannot die inside a pool child in process, so
these histories run the application's real worker, ``celery worker`` with the
prefork pool, as a child process of the test, and tell it where to fail
(:mod:`due_work_harness.process_histories`). Recovery is what production does
after any failure: the worker starting again.

The faults are Celery's, the same for every application, applied to the first
execution of one named task:

Deaths (the pool child ``os._exit``\\ s; the worker's main process lives on and
sees the child lost):

* ``task_prerun`` — as the task starts, before its body runs;
* ``mark_as_done`` — the body ran and its links were published; the result
  was not stored yet;
* ``task_postrun`` — the result was stored as SUCCESS; the parent has not heard.

Failures the worker survives:

* ``the task's on_success hook raised`` — the ``Task.on_success`` hook raises
  instead of running, as a hook with a bug or an unreachable service would;
* ``the broker refused the task's link`` — the first signature the task's
  execution publishes (a link, a chain's next step) is refused with kombu's
  ``OperationalError``, as a broker that is down would.

:func:`worker_history` builds the process history; its observation is the
adopter's, typically the task's result state with what its links and errbacks
did. Helpers import Celery only in the child.
"""

import importlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from due_work_harness.contract import (
    Adoption,
    Decline,
    DueWorkContract,
    NotApplicable,
    Profile,
    SafetyContract,
    SafetyProfile,
)
from due_work_harness.crash_histories import Findings
from due_work_harness.helpers import wait_until
from due_work_harness.integrations.task_queues import (
    replay_safety_is_the_functions,
    settled_by_one_worker,
    the_obligation_is_the,
)
from due_work_harness.process_histories import (
    FAULT_VARIABLE,
    ProcessHistory,
    fault_environment,
    fault_fires,
    fault_happened,
)

#: Where the pool child dies.
DEATH_POINTS = ("task_prerun", "mark_as_done", "task_postrun")
#: What fails while the worker lives on.
FAILURE_POINTS = ("the task's on_success hook raised", "the broker refused the task's link")

#: Which task the child fails; the fault itself travels in process_histories' protocol.
_TASK = "DUE_WORK_CELERY_TASK"


@contextmanager
def running_worker(
    app: str,
    *,
    fault: str | None = None,
    task: str | None = None,
    marker: Path | None = None,
    env: Mapping[str, str] | None = None,
    log: Path | None = None,
    worker_args: Sequence[str] = (),
) -> Iterator[subprocess.Popen[bytes]]:
    """
    The application's worker, ``celery worker`` with one prefork child, running for the block.

    ``app`` is the Celery application as ``module:attribute``. When ``fault``
    names a death or failure point, the first execution of ``task`` fails there
    and ``marker`` is created. On exit the worker gets a warm shutdown, as a
    deploy would, and is killed if it does not stop.
    """
    environ = {**os.environ, **(env or {}), **fault_environment(fault, marker), _TASK: task or ""}
    output = open(log, "ab") if log is not None else subprocess.DEVNULL  # noqa: SIM115 - closed below
    command = [sys.executable, "-m", "due_work_harness.integrations.celery_worker", app, *worker_args]
    process = subprocess.Popen(command, env=environ, stdout=output, stderr=subprocess.STDOUT)
    try:
        yield process
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if log is not None and not isinstance(output, int):
            output.close()


def worker_history[HandleT, ObservationT](
    *,
    name: str,
    app: str,
    task: str,
    send: Callable[[], HandleT],
    observe: Callable[[HandleT], ObservationT],
    initial: ObservationT,
    settled: Callable[[HandleT], bool],
    death_points: Sequence[str] = DEATH_POINTS,
    failure_points: Sequence[str] = FAILURE_POINTS,
    quiet: float = 1.0,
    timeout: float = 60.0,
    env: Mapping[str, str] | None = None,
    log: Path | None = None,
    findings: Findings | None = None,
) -> ProcessHistory[HandleT, ObservationT]:
    """
    One task through the application's real worker, failing at each of Celery's stages, recovered by a restart.

    ``send`` publishes the task (with its links and error callbacks) and returns
    what ``observe`` reads. ``settled`` says when the work it started is done;
    each run then waits ``quiet`` seconds more for anything it published late.
    A run reports status 1 when its fault happened, so a stage the task never
    reached is caught as a history that was never interrupted.
    """
    unknown = set(death_points) - set(DEATH_POINTS) | set(failure_points) - set(FAILURE_POINTS)
    assert not unknown, (
        f"{name}: no such Celery fault {sorted(unknown)}; the faults are {DEATH_POINTS + FAILURE_POINTS}"
    )

    def run(fault: str | None) -> tuple[HandleT, int]:
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "fault"
            with running_worker(app, fault=fault, task=task, marker=marker, env=env, log=log):
                handle = send()
                wait_until(lambda: settled(handle), timeout=timeout, what=f"{name}: the task did not settle")
                time.sleep(quiet)
            happened = fault_happened(marker)
        return handle, 0 if fault is None else int(happened)

    def recover(handle: HandleT) -> None:
        # REAL PRODUCTION: the worker starting again, then running until the work is settled.
        with running_worker(app, env=env, log=log):
            wait_until(lambda: settled(handle), timeout=timeout, what=f"{name}: recovery did not settle")
            time.sleep(quiet)

    return ProcessHistory(
        name=name,
        initial=initial,
        run=run,
        observe=observe,
        recover=recover,
        death_points=tuple(death_points),
        failure_points=tuple(failure_points),
        findings=findings,
    )


#: What Celery's worker does not promise, for any adopter's contract.
BROKER_REDELIVERS = (
    "Celery keeps no owed state of its own to sweep: a message the worker did not acknowledge is the broker's to "
    "redeliver (task_acks_late), and one it did acknowledge is settled"
)
BROKER_OWNS_LEASES = (
    "a task's lease is the broker's visibility timeout or connection, not a fenced claim Celery can check"
)
AT_LEAST_ONCE = "with task_acks_late a task whose worker died may run again; whether it reached its service is its own"
RESULTS_ARE_NOT_OWED = "stored results are the record of settled work, not work still owed"


def worker_contract(
    *,
    name: str,
    history: ProcessHistory[Any, Any],
    gap: str | None = None,
    fixtures: tuple[str, ...] = (),
) -> DueWorkContract:
    """
    Celery's contract with its worker: one task through the real worker, failing at each of Celery's stages.

    ``history`` is :func:`worker_history` for one of the adopter's tasks, a
    process handoff of the contract. What it finds is declared as ``gap``, a
    strict xfail, and, when the history carries its ``findings``, pinned
    history by history in the same run. The profiles Celery leaves to the
    broker or to the task are declined with the reason.
    """
    return DueWorkContract(
        name=name,
        adoption=Adoption.LEGACY,
        profiles={
            Profile.A: Decline(BROKER_REDELIVERS),
            Profile.B: Decline(BROKER_OWNS_LEASES),
            Profile.C: Decline(AT_LEAST_ONCE),
            Profile.D: NotApplicable(RESULTS_ARE_NOT_OWED),
            Profile.E: settled_by_one_worker("delivery"),
            Profile.F: the_obligation_is_the("message"),
        },
        safety=SafetyContract(
            name=name,
            profiles={
                SafetyProfile.REPLAY_SAFE_EXECUTION: replay_safety_is_the_functions(
                    "task", "Celery", runs_again="with task_acks_late, a redelivery after its worker was lost"
                ),
                SafetyProfile.BOUNDED_RETRY: Decline(
                    "a task's retries are its own (self.retry, autoretry_for); a redelivery after a lost worker is "
                    "not counted as one"
                ),
            },
        ),
        process_handoffs=(history,),
        handoff_gaps={history.name: gap} if gap is not None else {},
        fixtures=fixtures,
    )


def _install(app: Any, fault: str, task: str) -> None:
    """FAULT INJECTION, in the worker process before it forks its pool."""
    from celery import signals
    from celery.canvas import Signature
    from kombu.exceptions import OperationalError

    from due_work_harness.worker_death import ReceiverFailed

    def ours(name: str | None) -> bool:
        return name == task and fault_fires(fault)

    if fault == "task_prerun":

        def die_at_start(sender: Any = None, **_kwargs: Any) -> None:
            if ours(getattr(sender, "name", None)):
                os._exit(1)

        signals.task_prerun.connect(die_at_start, weak=False)
    elif fault == "task_postrun":

        def die_after_store(sender: Any = None, **_kwargs: Any) -> None:
            if ours(getattr(sender, "name", None)):
                os._exit(1)

        signals.task_postrun.connect(die_after_store, weak=False)
    elif fault == "mark_as_done":
        backend_class = type(app.backend)
        store = backend_class.mark_as_done

        def die_before_store(
            self: Any, task_id: str, result: Any, request: Any = None, *args: Any, **kwargs: Any
        ) -> Any:
            if ours(getattr(request, "task", None)):
                os._exit(1)
            return store(self, task_id, result, request, *args, **kwargs)

        backend_class.mark_as_done = die_before_store
    elif fault == "the task's on_success hook raised":
        target = app.tasks[task]

        def on_success(*_args: Any, **_kwargs: Any) -> None:
            if fault_fires(fault):
                raise ReceiverFailed(f"the on_success hook of {task} failed")

        type(target).on_success = on_success
    elif fault == "the broker refused the task's link":
        publish = Signature.apply_async

        def refusing(self: Any, *args: Any, **kwargs: Any) -> Any:
            if self.task != task and fault_fires(fault):
                raise OperationalError(f"the broker refused {self.task}: [Errno 111] Connection refused")
            return publish(self, *args, **kwargs)

        Signature.apply_async = refusing
    else:
        raise AssertionError(f"no such Celery fault {fault!r}")


def main(argv: Sequence[str]) -> None:
    """The child: import the application, install its one fault, run its worker."""
    module, _, attribute = argv[0].partition(":")
    app = getattr(importlib.import_module(module), attribute or "app")
    fault = os.environ.get(FAULT_VARIABLE, "")
    if fault:
        _install(app, fault, os.environ[_TASK])
    app.worker_main(
        [
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--loglevel=WARNING",
            "--without-mingle",
            "--without-gossip",
            "--without-heartbeat",
            *argv[1:],
        ]
    )


if __name__ == "__main__":
    main(sys.argv[1:])


__all__ = [
    "AT_LEAST_ONCE",
    "BROKER_OWNS_LEASES",
    "BROKER_REDELIVERS",
    "DEATH_POINTS",
    "FAILURE_POINTS",
    "RESULTS_ARE_NOT_OWED",
    "running_worker",
    "wait_until",
    "worker_contract",
    "worker_history",
]
