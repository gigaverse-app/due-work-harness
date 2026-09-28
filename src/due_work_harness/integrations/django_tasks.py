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

#: The signals db_worker installs its own handlers for.
_WORKER_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGQUIT") if hasattr(signal, name))


def db_worker_once(*, queues: str = "*", backend: str = "default") -> Callable[[], None]:
    """
    Recovery for a project on django-tasks-db: one ``manage.py db_worker --batch`` pass until no task is ready.

    ``db_worker`` installs its own SIGINT and SIGTERM handlers for graceful
    shutdown; they are restored afterwards, so the test process keeps its own.
    """

    def run() -> None:
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
