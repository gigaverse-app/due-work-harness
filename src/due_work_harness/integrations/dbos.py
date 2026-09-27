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
from collections.abc import Callable


def restart_until(
    launch: Callable[[], object],
    settled: Callable[[], bool],
    *,
    timeout: float = 60.0,
    poll: float = 0.2,
    shutdown_timeout: int = 5,
) -> None:
    """
    Run ``launch`` (the application's own startup, which calls ``DBOS.launch()``), then wait for ``settled``.

    ``launch`` must return once the application is up — replace a blocking
    server call at its seam. DBOS is destroyed afterwards whatever happens.
    """
    from dbos import DBOS

    try:
        launch()
        deadline = time.monotonic() + timeout
        while not settled():
            assert time.monotonic() < deadline, f"work was not settled within {timeout}s of restarting"
            time.sleep(poll)
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=shutdown_timeout)
