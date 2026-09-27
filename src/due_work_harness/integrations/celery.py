"""
Schedule evidence for recovery ticks run by Celery beat.

Profile A's ``assert_scheduled`` and ``tick_interval`` are scheduler-neutral: an
adopter supplies whatever proves its tick runs recurringly. These helpers cover
the common Celery case, reading the beat schedule from a Celery app's
``conf.beat_schedule`` when one is passed, otherwise from Django's
``CELERY_BEAT_SCHEDULE`` setting (Django is imported only then).
:func:`celery_publications` is a ``publication_recorder`` for hosts whose
workers publish through Celery, and :func:`held_publications` records each held
message with its arguments; both import Celery when entered.
"""

import importlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, NamedTuple
from unittest import mock


def _beat_schedule(app: Any | None) -> Mapping[str, Mapping[str, Any]]:
    if app is not None:
        return app.conf.beat_schedule
    from django.conf import settings

    return settings.CELERY_BEAT_SCHEDULE


def celery_beat_evidence(task_path: str, *, app: Any | None = None) -> Callable[[], None]:
    """
    Schedule evidence for the common case: an entry in the beat schedule.

    Checks two things, because the entry alone is not evidence that anything
    runs. A check that compares the adopter's string against the strings in the
    schedule and stops there is satisfied by a beat entry left pointing at a task
    that has been renamed or moved — while beat raises ``NotRegistered`` at every
    tick in production. That is precisely the "nothing runs it" failure profile
    A's invariant 8 exists to catch, and such a check cannot see it.

    So the path must also resolve to something callable. A dotted import is the
    portable check: Celery registers a task by importing the module that
    decorates it, so a path that cannot be imported cannot be a registered task
    either.
    """

    def check() -> None:
        scheduled = {entry.get("task") for entry in _beat_schedule(app).values()}
        assert task_path in scheduled, f"{task_path!r} is not in the beat schedule, so nothing runs it"

        module_path, _, attribute = task_path.rpartition(".")
        try:
            task = getattr(importlib.import_module(module_path), attribute)
        except (ImportError, AttributeError) as error:
            raise AssertionError(
                f"{task_path!r} is in the beat schedule but does not resolve to anything ({error!r}). "
                f"Beat will publish it and the worker will reject it as unregistered, so the sweep never "
                f"runs — which every other invariant would still pass"
            ) from error

        assert callable(task) or hasattr(task, "delay"), (
            f"{task_path!r} resolves to {task!r}, which is neither callable nor a Celery task, "
            f"so scheduling it runs nothing"
        )

    return check


def celery_beat_interval(task_path: str, *, app: Any | None = None) -> timedelta:
    """The tick interval for the common case, read from the beat schedule."""
    for entry in _beat_schedule(app).values():
        if entry.get("task") == task_path:
            schedule = entry["schedule"]
            assert isinstance(schedule, timedelta), (
                f"{task_path} is scheduled with {schedule!r}, which is not an interval this helper can convert"
            )
            return schedule
    raise AssertionError(f"{task_path!r} is not in the beat schedule")


class Publication(NamedTuple):
    """One Celery message a held publication recorded: the task, and the arguments it would carry."""

    task: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@contextmanager
def _holding(record: Callable[[Publication], object]) -> Iterator[None]:
    """Patch Celery's publication paths so each message is recorded and none is sent or run."""
    from celery import Celery
    from celery.app.task import Task
    from celery.result import AsyncResult

    count = [0]

    def held(publication: Publication) -> AsyncResult:
        record(publication)
        count[0] += 1
        return AsyncResult(f"held-{count[0]}")

    def apply_async(task: Any, args: Any = None, kwargs: Any = None, *_rest: Any, **_options: Any) -> AsyncResult:
        return held(Publication(task.name, tuple(args or ()), dict(kwargs or {})))

    def send_task(
        _app: Any, name: str, args: Any = None, kwargs: Any = None, *_rest: Any, **_options: Any
    ) -> AsyncResult:
        return held(Publication(name, tuple(args or ()), dict(kwargs or {})))

    with mock.patch.object(Task, "apply_async", apply_async), mock.patch.object(Celery, "send_task", send_task):
        yield


@contextmanager
def held_publications() -> Iterator[list[Publication]]:
    """
    Hold every Celery publication in the block, recording what it would have sent.

    Every ``Task.apply_async`` (and so ``delay``) and ``Celery.send_task`` appends
    a :class:`Publication` and returns without reaching a broker or running the
    task, even under ``task_always_eager``: work handed off only as a message is
    visible, and is never delivered.
    """
    held: list[Publication] = []
    with _holding(held.append):
        yield held


@contextmanager
def celery_publications() -> Iterator[list[str]]:
    """
    Record Celery publications without sending them: the host's ``publication_recorder``.

    The task names of :func:`held_publications`, for proofs that ask only what
    was handed off.
    """
    names: list[str] = []
    with _holding(lambda publication: names.append(publication.task)):
        yield names
