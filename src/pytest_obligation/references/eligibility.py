"""
An independent in-memory scheduler, for testing the execution-eligibility proofs themselves.

:class:`GateReference` owns one obligation that is blocked until something makes
it eligible, with an optional periodic inspection as the fallback when the
readiness notification is lost. It is the conforming implementation of what
:class:`~pytest_obligation.profiles.gated_execution.ExecutionGate` binds, in
three shapes: an inspection that counts as an execution (the default), one
observed apart (``separate_inspections``), and a clock that moves only in whole
seconds (``whole_seconds``, conforming once its resolution is declared). Each
``fault`` breaks exactly one thing the proofs must catch:

* ``hidden_write``, ``lost_intent``, ``mutated_intent``, ``blocked_execution`` —
  a blocked run that writes, drops the obligation, rewrites its revision, or executes;
* ``reset_on_readiness`` — becoming eligible rewrites the reserved intent;
* ``no_recovery`` — an eligible obligation is never executed;
* ``fake_completion`` — completion that never reaches the provider;
* ``no_fallback``, ``early_fallback``, ``hot_loop``, ``short_rearm``,
  ``one_shot_fallback`` — the periodic inspection never happens, happens a
  second early, never advances its own next time, re-arms one second later
  instead of ``recheck_after``, or happens once and never again;
* ``runs_at_inspection``, ``completes_at_inspection``, ``drops_at_inspection``,
  ``executes_at_inspection`` — the inspection calls the provider, completes the
  blocked work, drops it, or (observed apart) executes it.

:meth:`GateReference.sweep` is the contract sweep over the same scheduler: its selection, and a tick
that records what it dispatched. ``rearm_delay`` pushes each re-inspection later than ``recheck_after``,
as a continuation delay does; the scheduler still conforms.

Never bind this in an adopter.
"""

from datetime import timedelta
from typing import Literal

from pytest_obligation.helpers import undeclared
from pytest_obligation.models import MutableHarnessModel
from pytest_obligation.profiles.automatic_recovery import DueWorkSweep
from pytest_obligation.profiles.gated_execution import ExecutionGate

_INSPECTION = timedelta(seconds=30)

#: Every fault the reference can inject; a misspelled one is refused, not silently conforming.
Fault = Literal[
    "",
    "hidden_write",
    "lost_intent",
    "mutated_intent",
    "blocked_execution",
    "reset_on_readiness",
    "no_recovery",
    "fake_completion",
    "no_fallback",
    "early_fallback",
    "hot_loop",
    "short_rearm",
    "one_shot_fallback",
    "runs_at_inspection",
    "completes_at_inspection",
    "drops_at_inspection",
    "executes_at_inspection",
]


class GateReference(MutableHarnessModel):
    """One blocked obligation and the scheduler that releases it, with an optional injected fault."""

    fault: Fault = ""
    recheck_after: timedelta | None = _INSPECTION
    separate_inspections: bool = False
    whole_seconds: bool = False
    clock_resolution: timedelta = timedelta(microseconds=1)
    rearm_delay: timedelta = timedelta(0)
    elapsed: timedelta = timedelta(0)
    next_inspection: timedelta = _INSPECTION
    eligible: bool = False
    retained: bool = True
    revision: int = 1
    executions: int = 0
    inspections: int = 0
    calls: int = 0
    mutations: int = 0
    completed: bool = False
    dispatched: list[str] = []

    def due(self) -> list[str]:
        periodic = self.recheck_after is not None and self.elapsed >= self.next_inspection
        if self.fault == "no_fallback":
            periodic = False
        if self.fault == "early_fallback":
            periodic = self.elapsed >= self.next_inspection - timedelta(seconds=1)
        return ["obligation"] if not self.completed and (self.eligible or periodic) else []

    def owed(self) -> list[str]:
        return ["obligation"] if self.retained else []

    def execute(self) -> int:
        if not self.due():
            if self.fault == "hidden_write":
                self.mutations += 1
            if self.fault == "lost_intent":
                self.retained = False
            if self.fault == "mutated_intent":
                self.revision += 1
            if self.fault == "blocked_execution":
                self.executions += 1
            return 0
        if self.fault == "no_recovery":
            return 0
        if self.eligible:
            self.complete_directly()
        else:
            self._inspect()
        return 1

    def complete_directly(self) -> None:
        """The worker itself, invoked without the sweep: what a gate bound past the sweep would call."""
        if self.completed:
            return
        self.executions += 1
        self.completed = True
        if self.fault != "fake_completion":
            self.calls += 1

    def _inspect(self) -> None:
        if self.separate_inspections:
            self.inspections += 1
        else:
            self.executions += 1
        if self.fault == "executes_at_inspection":
            self.executions += 1
        if self.fault == "runs_at_inspection":
            self.calls += 1
        if self.fault == "completes_at_inspection":
            self.completed = True
        if self.fault == "drops_at_inspection":
            self.retained = False
        if self.fault == "hot_loop":
            return
        assert self.recheck_after is not None
        if self.fault == "one_shot_fallback":
            self.next_inspection = timedelta.max
        elif self.fault == "short_rearm":
            self.next_inspection = self.elapsed + timedelta(seconds=1)
        else:
            self.next_inspection = self.elapsed + self.recheck_after + self.rearm_delay

    def tick(self) -> int:
        """The sweep: dispatch everything due, recording what it dispatched, and run it."""
        self.dispatched = self.due()
        for _ in self.dispatched:
            self.execute()
        return len(self.dispatched)

    def make_eligible(self) -> None:
        self.eligible = True
        if self.fault == "reset_on_readiness":
            self.revision += 1

    def advance(self, delta: timedelta) -> None:
        if self.whole_seconds:
            delta = timedelta(seconds=int(delta.total_seconds()))
        self.elapsed += delta

    def binding(self) -> ExecutionGate[str, int, bool]:
        return ExecutionGate(
            name="independent in-memory scheduler",
            identity="obligation",
            due_work=self.due,
            owed_work=self.owed,
            reserved_state=lambda: self.revision,
            routes={"execute": self.execute},
            make_eligible=self.make_eligible,
            recover=self.tick,
            advance=self.advance,
            executions=lambda: self.executions,
            inspections=(lambda: self.inspections) if self.separate_inspections else None,
            mutation_count=lambda: self.mutations,
            observe=lambda: self.completed,
            expected=True,
            effect_calls=lambda: self.calls,
            recovery_interval=timedelta(seconds=5),
            recovery_timeout=timedelta(seconds=60),
            recheck_after=self.recheck_after,
            clock_resolution=self.clock_resolution,
        )

    def sweep(self) -> DueWorkSweep:
        """The contract sweep over this scheduler: its selection, its tick, and what the tick dispatched."""
        refuse = undeclared("only the sweep's selection and tick are compared with the gate")
        return DueWorkSweep(
            name="independent in-memory scheduler's sweep",
            due_work=self.due,
            run_tick=self.tick,
            make_owed=refuse,
            make_terminal=refuse,
            recovery_delay=None,
            page_size=None,
            dispatched_ids=lambda: list(self.dispatched),
        )

    def reset(self) -> None:
        """Back to one fresh blocked obligation, as a test database is for each case."""
        for name, field in type(self).model_fields.items():
            setattr(self, name, field.get_default(call_default_factory=True))


#: The scheduler the contract factories below share, as production code and its test share a database:
#: :func:`reference_gate` arranges a fresh blocked obligation in it, and :func:`reference_sweep` sweeps
#: whatever it holds, so the two may be built in either order.
SCHEDULER = GateReference()


def reference_gate() -> ExecutionGate[str, int, bool]:
    """A fresh blocked obligation in :data:`SCHEDULER`, for every generated contract case."""
    SCHEDULER.reset()
    return SCHEDULER.binding()


def reference_sweep() -> DueWorkSweep:
    """The contract sweep over :data:`SCHEDULER`."""
    return SCHEDULER.sweep()
