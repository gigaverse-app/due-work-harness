"""
An independent in-memory scheduler, for testing the execution-eligibility proofs themselves.

:class:`GateReference` owns one obligation that is blocked until something makes
it eligible, with an optional periodic inspection as the fallback when the
readiness notification is lost. It is the conforming implementation of what
:class:`~due_work_harness.profiles.execution_eligibility.ExecutionGate` binds, in
three shapes: an inspection that counts as an execution (the default), one
observed apart (``separate_inspections``), and a clock that moves only in whole
seconds (``whole_seconds``, conforming once its resolution is declared). Each
``fault`` breaks exactly one thing the proofs must catch:

* ``hidden_write``, ``lost_intent``, ``mutated_intent``, ``blocked_execution`` —
  a blocked run that writes, drops the obligation, rewrites its revision, or executes;
* ``reset_on_readiness`` — becoming eligible rewrites the reserved intent;
* ``no_recovery`` — an eligible obligation is never executed;
* ``fake_completion`` — completion that never reaches the provider;
* ``no_fallback``, ``early_fallback``, ``hot_loop``, ``short_rearm`` — the
  periodic inspection never happens, happens a second early, never advances its
  own next time, or re-arms one second later instead of ``recheck_after``;
* ``runs_at_inspection``, ``completes_at_inspection``, ``drops_at_inspection``,
  ``executes_at_inspection`` — the inspection calls the provider, completes the
  blocked work, drops it, or (observed apart) executes it.

:meth:`GateReference.sweep` is the contract sweep over the same scheduler.

Never bind this in an adopter.
"""

from datetime import timedelta

from due_work_harness.helpers import undeclared
from due_work_harness.models import MutableHarnessModel
from due_work_harness.profiles.automatic_recovery import DueWorkSweep
from due_work_harness.profiles.execution_eligibility import ExecutionGate

_INSPECTION = timedelta(seconds=30)


class GateReference(MutableHarnessModel):
    """One blocked obligation and the scheduler that releases it, with an optional injected fault."""

    fault: str = ""
    recheck_after: timedelta | None = _INSPECTION
    separate_inspections: bool = False
    whole_seconds: bool = False
    clock_resolution: timedelta = timedelta(microseconds=1)
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
        self.next_inspection = self.elapsed + (
            timedelta(seconds=1) if self.fault == "short_rearm" else self.recheck_after
        )

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
            recover=self.execute,
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
        """The contract sweep over this scheduler: its selection, and a tick that runs it."""
        refuse = undeclared("only the sweep's selection and tick are compared with the gate")
        return DueWorkSweep(
            name="independent in-memory scheduler's sweep",
            due_work=self.due,
            run_tick=self.execute,
            make_owed=refuse,
            make_terminal=refuse,
            recovery_delay=None,
            page_size=None,
        )


#: The scheduler :func:`reference_gate` built last, which :func:`reference_sweep` sweeps: a contract calls
#: its gate factory and then its sweep factory for one case, and both must describe the same scheduler.
_LATEST: list[GateReference] = []


def reference_gate() -> ExecutionGate[str, int, bool]:
    """A fresh gate for every generated contract case, without database fixtures."""
    _LATEST[:] = [GateReference()]
    return _LATEST[0].binding()


def latest_reference() -> GateReference:
    """The scheduler behind the gate :func:`reference_gate` built last."""
    assert _LATEST, "reference_gate() has not built a gate yet"
    return _LATEST[0]


def reference_sweep() -> DueWorkSweep:
    """The contract sweep over the scheduler :func:`reference_gate` built last."""
    return latest_reference().sweep()
