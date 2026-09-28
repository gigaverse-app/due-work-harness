"""
Counted hooks: what every receiver or callback breaker does, whatever the framework.

A framework calls hooks around the work it runs: Django's signal receivers, a
task queue's success and failure callbacks. A crash history fails each in turn
while the process lives on. :class:`CountedHooks` is that behaviour once: it
wraps each hook as the framework calls it, counts it, and makes the chosen one
raise :class:`~due_work_harness.worker_death.ReceiverFailed` instead of running.
What happens next is the framework's own. An integration only finds its hooks.
"""

from collections.abc import Callable
from typing import Any

from due_work_harness.worker_death import ReceiverFailed


class CountedHooks:
    """The hooks a crash history fails: how many ran, and the failure raised (a ``CountedFaults``)."""

    def __init__(self, fail_at: int | None, *, kind: str = "signal receiver") -> None:
        self._fail_at = fail_at
        self._kind = kind
        self.count = 0
        self.failure: ReceiverFailed | None = None

    def counted(self, hook: Callable[..., Any]) -> Callable[..., Any]:
        """``hook`` as the framework will call it: counted, and failing when it is the chosen one."""

        def run(*args: Any, **kwargs: Any) -> Any:
            self.count += 1
            if self.count == self._fail_at:
                name = getattr(hook, "__qualname__", repr(hook))
                self.failure = ReceiverFailed(f"{self._kind} {self.count} ({name}) failed")
                raise self.failure
            return hook(*args, **kwargs)

        return run


__all__ = ["CountedHooks"]
