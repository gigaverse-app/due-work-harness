"""
DBOS integration: restart an application the way production would, then settle.

DBOS runs workflows on its own executor threads and recovers pending workflows
when the application launches again. That makes the honest crash history a
real process death (:mod:`due_work_harness.process_histories`) followed by the
application's own startup. :func:`restart_until` is that startup as a recovery
binding: it runs the application's launch path, waits until the work is
settled, then shuts DBOS down as a process exit would, so the next history
starts from a clean runtime.

DBOS resumes an interrupted workflow from its last *completed* step. A step
that performed an external effect and died before its result was recorded runs
again on recovery. Crash histories make that visible when the observation
includes what the external system saw.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from due_work_harness.helpers import wait_until
from due_work_harness.profiles.durable_retention import Retention

#: Workflow statuses DBOS will still run: the work they owe is outstanding.
OUTSTANDING = ("PENDING", "ENQUEUED")


@contextmanager
def launched(launch: Callable[[], object], *, shutdown_timeout: int = 5) -> Iterator[None]:
    """
    The application launched through its own startup (which calls ``DBOS.launch()``), shut down on exit.

    ``launch`` must return once the application is up: replace a blocking
    server call at its seam. DBOS is destroyed afterwards whatever happens, as a
    process exit would, so the next launch starts from a clean runtime.
    """
    from dbos import DBOS

    try:
        launch()
        yield
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=shutdown_timeout)


def restart_until(
    launch: Callable[[], object],
    settled: Callable[[], bool],
    *,
    timeout: float = 60.0,
    poll: float = 0.2,
    shutdown_timeout: int = 5,
) -> None:
    """Run the application's startup (see :func:`launched`), then wait until ``settled``."""
    with launched(launch, shutdown_timeout=shutdown_timeout):
        wait_until(settled, timeout=timeout, poll=poll, what="work was not settled")


def workflow_status(workflow_id: str) -> str | None:
    """A workflow's status as DBOS records it, or ``None`` once it no longer exists."""
    from dbos import DBOS

    status = DBOS.get_workflow_status(workflow_id)
    return None if status is None else status.status


def retention(*, make_owed: Callable[[], str], make_finished: Callable[[], str]) -> Retention:
    """
    Profile D bound to DBOS's own retention: ``garbage_collect``, as its admin endpoint and conductor run it.

    ``make_owed`` and ``make_finished`` return workflow ids, through the
    application's own workflows: one still outstanding, one completed. The
    pass runs with a cutoff a minute ahead, the most aggressive window: every
    completed workflow is prunable (whatever the skew between this clock and
    the database's, which stamps ``completed_at``), and an outstanding one has
    no ``completed_at`` to compare. DBOS must be launched in this process (see
    :func:`launched`).
    """
    from dbos._dbos import _get_dbos_instance
    from dbos._workflow_commands import garbage_collect

    def collect() -> None:
        garbage_collect(
            _get_dbos_instance(), cutoff_epoch_timestamp_ms=int((time.time() + 60) * 1000), rows_threshold=None
        )

    return Retention(
        name="DBOS garbage_collect",
        make_non_terminal=make_owed,
        make_prunable=make_finished,
        run_retention=collect,
        still_exists=lambda workflow_id: workflow_status(workflow_id) is not None,
    )
