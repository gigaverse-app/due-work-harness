"""
Count the callbacks ``transaction.on_commit`` runs on Django's default connection, and fail one.

This is the Django host's :class:`~pytest_obligation.host.CallbackBreaker`.
Each callback registered on the calling thread's default connection is wrapped
as it is registered and counted as it runs, so one discarded with a rolled-back
savepoint never counts. The chosen callback raises
:class:`~pytest_obligation.worker_death.CallbackFailed` instead of running, and
Django does the rest as it would in production: a failing non-robust callback
stops every later callback of the same commit and re-raises to the caller; a
``robust=True`` callback's failure is logged and the next one runs.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.base import BaseDatabaseWrapper

from pytest_obligation.worker_death import CallbackFailed


class DjangoCallbacks:
    """The after-commit callbacks a crash history fails: how many ran, and whether the chosen one failed."""

    def __init__(self, fail_at: int | None) -> None:
        self._fail_at = fail_at
        self.count = 0
        self.failure: CallbackFailed | None = None

    def run(self, callback: Callable[[], Any]) -> Any:
        self.count += 1
        if self.count == self._fail_at:
            name = getattr(callback, "__qualname__", repr(callback))
            self.failure = CallbackFailed(f"after-commit callback {self.count} ({name}) failed")
            raise self.failure
        return callback()


@contextmanager
def django_callback_breaker(fail_at: int | None) -> Iterator[DjangoCallbacks]:
    """Count after-commit callbacks on this thread's default connection; fail callback ``fail_at``."""
    target = connections[DEFAULT_DB_ALIAS]
    callbacks = DjangoCallbacks(fail_at)
    original = type(target).on_commit

    def on_commit(connection: BaseDatabaseWrapper, func: Callable[[], Any], robust: bool = False) -> Any:
        if connection is not target:
            return original(connection, func, robust=robust)
        return original(connection, lambda: callbacks.run(func), robust=robust)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(target), "on_commit", on_commit)
        yield callbacks
