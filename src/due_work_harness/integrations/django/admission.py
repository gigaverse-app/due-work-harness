"""Fault injection after a real admission write, without supplying its transaction."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from django.db import connection


class AdmissionInterrupted(RuntimeError):
    """The deliberate failure injected after both halves of admission exist."""


@contextmanager
def interrupt_after_statement(matches: Callable[[str], bool], checkpoint: Callable[[], None]) -> Iterator[None]:
    """Interrupt after the first matching SQL statement succeeds on the default connection.

    The caller identifies the final partial write, usually the obligation INSERT.
    The proof's checkpoint reads through the same connection, so it sees the real
    uncommitted rows. This scope only removes instrumentation on exit: production
    must own commit/rollback, and assertion failures must escape unchanged.
    """
    reached = False

    def execute(run: Callable[..., Any], sql: str, params: Any, many: bool, context: Any) -> Any:
        nonlocal reached
        result = run(sql, params, many, context)
        if not reached and matches(sql):
            reached = True  # Checkpoint queries must not recursively fire the fault.
            checkpoint()
            raise AdmissionInterrupted("interrupted after the real admission write")
        return result

    with connection.execute_wrapper(execute):
        yield
