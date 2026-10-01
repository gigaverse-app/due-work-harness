"""
External host interfaces: retain provider requests and control captured deliveries.

Implementations own real host resources; runtime protocol validation checks the
interface shape without copying its mutable state. Behavioral obligations are
checked by the engine and application contract tests.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class PendingRequest(Protocol):
    """Host-owned accepted request whose external effect may finish after its caller fails."""

    completed: bool
    """True after external completion; the engine requires every held request to finish."""

    def complete(self) -> None:
        """Apply the retained external effect using the host request owner's completion rules."""
        ...


@runtime_checkable
class Transport(Protocol):
    """External notification controls; implementations must not edit durable work rows."""

    def drop(self, enabled: bool) -> None:
        """Toggle notification loss; enabling loss also discards currently held messages."""
        ...

    def redeliver(self, index: int) -> None:
        """Deliver the zero-based captured worker message again, preserving its original payload."""
        ...
