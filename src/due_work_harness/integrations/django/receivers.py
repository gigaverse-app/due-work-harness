"""
Count the receivers that chosen Django signals run, and fail one.

This is the Django host's :class:`~due_work_harness.host.ReceiverBreaker`,
built for the signals an adopter names: a task framework's ``task_started`` and
``task_finished``, a CMS's ``page_published``, a model's ``post_save``. Each
live synchronous receiver of those signals is counted as it runs. The chosen one
raises :class:`~due_work_harness.worker_death.ReceiverFailed` instead of
running, as a receiver with a bug or an unreachable backend would, and Django
does the rest as in production: ``Signal.send`` propagates the failure to the
sender and skips the receivers after it; ``send_robust`` logs it and runs the
next one.
"""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock

from django.dispatch import Signal

from due_work_harness.host import ReceiverBreaker
from due_work_harness.worker_death import ReceiverFailed


class DjangoReceivers:
    """The signal receivers a crash history fails: how many ran, and the failure raised."""

    def __init__(self, fail_at: int | None) -> None:
        self._fail_at = fail_at
        self.receivers = 0
        self.failure: ReceiverFailed | None = None

    def counted(self, receiver: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            self.receivers += 1
            if self.receivers == self._fail_at:
                name = getattr(receiver, "__qualname__", repr(receiver))
                self.failure = ReceiverFailed(f"signal receiver {self.receivers} ({name}) failed")
                raise self.failure
            return receiver(*args, **kwargs)

        return run


def django_receiver_breaker(*signals: Signal) -> ReceiverBreaker:
    """A ``receiver_breaker`` for ``django_host``: count the receivers of ``signals`` and fail receiver ``fail_at``."""
    assert signals, "name the signals whose receivers crash histories should fail"

    @contextmanager
    def breaker(fail_at: int | None) -> Iterator[DjangoReceivers]:
        receivers = DjangoReceivers(fail_at)
        with ExitStack() as stack:
            for signal in signals:
                live = signal._live_receivers

                def counting(sender: Any, live: Callable[[Any], Any] = live) -> Any:
                    synchronous, asynchronous = live(sender)
                    return [receivers.counted(receiver) for receiver in synchronous], asynchronous

                stack.enter_context(mock.patch.object(signal, "_live_receivers", counting))
            yield receivers

    return breaker
