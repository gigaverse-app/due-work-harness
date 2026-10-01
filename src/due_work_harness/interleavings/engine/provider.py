"""External-only controls; application admission and recovery remain untouched."""

from collections import Counter, deque
from collections.abc import Callable
from typing import TypeVar, cast

from pydantic import Field, InstanceOf

from due_work_harness.models import MutableHarnessModel

from ..model import Fault
from ..ports import PendingRequest

T = TypeVar("T")


class ProviderControl(MutableHarnessModel):
    """
    Mutable external-fault controller, reusing the host's accepted-request owner.

    Live queues and counters belong to one session; injected accepted requests
    retain their host ownership and are never copied during validation.
    Each session must receive a fresh instance; all ledger keys are (seam, identity).
    """

    accept: Callable[[Callable[[], None]], PendingRequest] = lambda apply: AcceptedProviderRequest(apply=apply)
    """Host callback retaining an external-only completion function beyond caller failure."""
    pending: list[InstanceOf[PendingRequest]] = Field(default_factory=list)
    """Accepted requests in stable index order, including those already completed."""
    calls: Counter[tuple[str, str]] = Field(default_factory=Counter)
    """Attempted provider invocations, counted even when refused before application."""
    effects: Counter[tuple[str, str]] = Field(default_factory=Counter)
    """Effects perform() actually applied to the external fake, including those whose responses were lost."""
    unapplied_acknowledgements: Counter[tuple[str, str]] = Field(default_factory=Counter)
    """Calls acknowledged without applying; never counted in effects."""
    armed: deque[tuple[str, Fault]] = Field(default_factory=deque)
    """At most one pending injection; only the matching seam consumes it."""
    reached: list[tuple[str, Fault]] = Field(default_factory=list)
    """Ordered audit of injections that actually reached the provider boundary."""

    def arm(self, seam: str, fault: Fault) -> None:
        """Arm exactly one boundary; fail if a previous injection was never exercised."""
        assert not self.armed, "previous injected boundary was never reached"
        self.armed.append((seam, fault))

    def invoke(self, seam: str, identity: str, perform: Callable[[], T], reply: Callable[[], T] | None = None) -> T:
        """
        Invoke an external fake effect under the armed fault; perform must never mutate application state.

        reply builds the provider's acknowledgement without applying anything, returned
        under ACKNOWLEDGE_WITHOUT_APPLYING; seams whose perform returns None may omit it.
        The fault is one-shot: the next invocation at the seam may apply normally.
        """
        key = (seam, identity)
        self.calls[key] += 1
        fault = None
        if self.armed and self.armed[0][0] == seam:
            _, fault = self.armed.popleft()
            self.reached.append((seam, fault))
        if fault == Fault.REFUSE:
            raise TimeoutError("interleaving: provider refused before application")
        if fault == Fault.ACKNOWLEDGE_WITHOUT_APPLYING:
            # The caller sees an ordinary return; only the provider's state can
            # show that nothing was applied, and perform never runs.
            self.unapplied_acknowledgements[key] += 1
            return reply() if reply is not None else cast("T", None)

        def apply() -> T:
            result = perform()
            self.effects[key] += 1
            return result

        # Acceptance outlives the caller. The retained callback changes only the
        # external fake; it cannot acknowledge or repair application state.
        if fault == Fault.HOLD:

            def complete() -> None:
                apply()

            self.pending.append(self.accept(complete))
            raise TimeoutError("interleaving: accepted request is still pending")
        result = apply()
        # Losing the response must not roll back a provider effect already applied.
        if fault == Fault.LOSE_RESPONSE:
            raise TimeoutError("interleaving: applied; response lost")
        return result

    def complete(self, index: int) -> None:
        """Complete a previously accepted external request at its stable zero-based index."""
        assert index < len(self.pending), "history never reached the required accepted request"
        self.pending[index].complete()

    def assert_reached(self) -> None:
        """Reject a vacuous history whose promised injection never reached the external seam."""
        assert not self.armed, "history did not reach the declared provider seam"


class AcceptedProviderRequest(MutableHarnessModel):
    """A retained external effect whose lifetime exceeds its caller's response."""

    apply: Callable[[], None]
    completed: bool = False

    def lose_response(self) -> None:
        """Lose the caller's response without cancelling the accepted effect."""
        raise TimeoutError("Provider accepted request; local response lost")

    def complete(self) -> None:
        """Apply exactly once; an accepted request cannot be completed twice."""
        assert not self.completed, "accepted request already completed"
        self.apply()
        self.completed = True
