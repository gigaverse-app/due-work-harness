"""The Celery worker histories refuse, in words, a platform whose prefork pool cannot run them."""

import sys

import pytest

from due_work_harness.integrations.celery_worker import running_worker


def test_the_worker_is_refused_on_windows_instead_of_timing_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # On Windows billiard's pool spawns its child, which inherits neither the fault installed in the
    # worker's main process nor working pool semaphores: the task never settles, and the history would
    # fail a minute later on a timeout that names nothing.
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(AssertionError, match="prefork pool.*does not run on Windows"), running_worker("any:app"):
        pass
