"""
A Celery task bound as a probe is read as its body, whatever object Celery hands back.

``shared_task`` returns a lazy proxy, which with ``bind=True`` wraps the task's bound
``run``; a class-based task is an instance whose body is its ``run`` method. The
binding tripwires must read that body, not ``Proxy.__call__`` or ``Task.__call__``,
or an inversion inside it goes unseen. No broker is needed: nothing is sent.
"""

from typing import Any

import pytest
from celery import Celery, Task, shared_task

from pytest_obligation.binding import authored_inversion_names, callable_code

app = Celery("due_work_binding_probes")


@shared_task(bind=True)
def swallowing_bound_shared_task(self: Task, proof: int) -> None:
    try:
        assert proof == 0
    except Exception:  # noqa: BLE001 - the inversion under test
        return


@shared_task
def swallowing_shared_task(proof: int) -> None:
    try:
        assert proof == 0
    except Exception:  # noqa: BLE001 - the inversion under test
        return


class SwallowingTask(Task):
    name = "due_work_binding_probes.swallowing"

    def run(self, proof: int) -> None:
        try:
            assert proof == 0
        except Exception:  # noqa: BLE001 - the inversion under test
            return


swallowing_class_task = app.register_task(SwallowingTask())


@pytest.mark.parametrize(
    ("task", "body"),
    [
        (swallowing_bound_shared_task, "swallowing_bound_shared_task"),
        (swallowing_shared_task, "swallowing_shared_task"),
        (swallowing_class_task, "SwallowingTask.run"),
    ],
    ids=["shared-task-bind", "shared-task", "class-based-task"],
)
def test_a_celery_task_is_read_as_its_body(task: Any, body: str) -> None:
    code = callable_code(task)
    assert code is not None and code.co_qualname == body
    assert "AssertionError" in authored_inversion_names(task)
