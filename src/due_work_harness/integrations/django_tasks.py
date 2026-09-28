"""
django-tasks integration: the database backend's worker as recovery.

django-tasks-db stores each enqueued task as a row, so a task enqueued inside
the product transaction commits or rolls back with it: there is no message
separate from the database to lose. Its worker, ``manage.py db_worker``, claims
a READY row, marks it RUNNING, runs it and records the outcome. What an
application using it still owes itself:

* **enqueuing in the same transaction** as the state that owes the task — a
  task enqueued from ``transaction.on_commit`` is lost with a process that dies
  between the commit and the callback (crash histories find this);
* **recovering a task whose worker died** — the worker selects only READY
  rows, so a task left RUNNING by a dead worker is never run again.

Helpers here import Django lazily, so importing this module never requires it.
"""

import signal
from collections.abc import Callable
from typing import Any

#: The signals db_worker installs its own handlers for.
_WORKER_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGQUIT") if hasattr(signal, name))


def db_worker_once(*, queues: str = "*", backend: str = "default") -> Callable[..., None]:
    """
    Recovery for a project on django-tasks-db: one ``manage.py db_worker --batch`` pass until no task is ready.

    The same pass is a handoff's transition when the worker itself is under
    test (it ignores the history's handle). ``db_worker`` installs its own SIGINT
    and SIGTERM handlers for graceful shutdown; they are restored afterwards, so
    the test process keeps its own.
    """

    def run(*_handle: object) -> None:
        from django.core.management import call_command

        handlers = {signum: signal.getsignal(signum) for signum in _WORKER_SIGNALS}
        try:
            call_command(
                "db_worker",
                "--batch",
                "--no-startup-delay",
                "--no-reload",
                "--queue-name",
                queues,
                "--backend",
                backend,
                verbosity=0,
            )
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)

    return run


def strand_as_running(task_id: str, *, worker_id: str = "a-worker-that-died") -> str:
    """FAULT INJECTION: make ``task_id`` look claimed by a worker that then died — RUNNING, started, owned by it."""
    from django.utils import timezone
    from django_tasks_db.models import DBTaskResult

    DBTaskResult.objects.filter(id=task_id).update(status="RUNNING", started_at=timezone.now(), worker_ids=[worker_id])
    return task_id


def tasks_run_by(tick: Callable[[], object]) -> list[str]:
    """The ids of the tasks ``tick`` started, as django-tasks-db's own ``task_started`` signal reports them."""
    from django_tasks_db.compat import task_started

    started: list[str] = []

    def record(sender: object, task_result: Any, **kwargs: object) -> None:
        started.append(str(task_result.id))

    task_started.connect(record, weak=False)
    try:
        tick()
    finally:
        task_started.disconnect(record)
    return started
