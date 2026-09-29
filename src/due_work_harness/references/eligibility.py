"""
An independent in-memory scheduler, for testing the execution-eligibility proofs themselves.

:class:`GateReference` owns one obligation that is blocked until something makes
it eligible, with an optional periodic inspection as the fallback when the
readiness notification is lost. It is the conforming implementation of what
:class:`~due_work_harness.profiles.execution_eligibility.ExecutionGate` binds,
and each ``fault`` breaks exactly one thing the proofs must catch:

* ``hidden_write``, ``lost_intent``, ``mutated_intent``, ``blocked_execution`` —
  a blocked run that writes, drops the obligation, rewrites its revision, or executes;
* ``reset_on_readiness`` — becoming eligible rewrites the reserved intent;
* ``no_recovery`` — an eligible obligation is never executed;
* ``fake_completion`` — completion that never reaches the provider;
* ``no_fallback``, ``early_fallback``, ``hot_loop`` — the periodic inspection
  never happens, happens a second early, or never advances its own next time.

Never bind this in an adopter.
"""

from datetime import timedelta

from due_work_harness.models import MutableHarnessModel
from due_work_harness.profiles.execution_eligibility import ExecutionGate

_INSPECTION = timedelta(seconds=30)


class GateReference(MutableHarnessModel):
    """One blocked obligation and the scheduler that releases it, with an optional injected fault."""

    fault: str = ""
    recheck_after: timedelta | None = _INSPECTION
    elapsed: timedelta = timedelta(0)
    next_inspection: timedelta = _INSPECTION
    eligible: bool = False
    retained: bool = True
    revision: int = 1
    executions: int = 0
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

    def execute(self) -> None:
        if not self.due():
            if self.fault == "hidden_write":
                self.mutations += 1
            if self.fault == "lost_intent":
                self.retained = False
            if self.fault == "mutated_intent":
                self.revision += 1
            if self.fault == "blocked_execution":
                self.executions += 1
            return
        if self.fault == "no_recovery":
            return
        self.executions += 1
        if self.eligible:
            self.completed = True
            if self.fault != "fake_completion":
                self.calls += 1
        elif self.fault != "hot_loop":
            assert self.recheck_after is not None
            self.next_inspection = self.elapsed + self.recheck_after

    def make_eligible(self) -> None:
        self.eligible = True
        if self.fault == "reset_on_readiness":
            self.revision += 1

    def advance(self, delta: timedelta) -> None:
        self.elapsed += delta

    def binding(self) -> ExecutionGate[str, int, bool]:
        return ExecutionGate(
            name="independent in-memory scheduler",
            identity="obligation",
            due_work=self.due,
            owed_work=lambda: ["obligation"] if self.retained else [],
            reserved_state=lambda: self.revision,
            routes={"execute": self.execute},
            make_eligible=self.make_eligible,
            recover=self.execute,
            advance=self.advance,
            executions=lambda: self.executions,
            mutation_count=lambda: self.mutations,
            observe=lambda: self.completed,
            expected=True,
            effect_calls=lambda: self.calls,
            recovery_interval=timedelta(seconds=5),
            recovery_timeout=timedelta(seconds=60),
            recheck_after=self.recheck_after,
        )


def reference_gate() -> ExecutionGate[str, int, bool]:
    """A fresh gate for every generated contract case, without database fixtures."""
    return GateReference().binding()
