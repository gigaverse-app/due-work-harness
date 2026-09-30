"""A queued automatic image crop must not overwrite an editor's newer focal point."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

from django.test import Client, override_settings
from django_tasks_db.models import DBTaskResult
from wagtail.images import get_image_model
from wagtail.images.tasks import set_image_focal_point_task
from willow import Image as WillowImage

from due_work_harness import ReplaySafeEffect
from due_work_harness.profiles.eventual_convergence import SupersededSnapshot


@contextmanager
def focal_point_snapshot(arrange: Callable[[], Any]) -> Iterator[SupersededSnapshot]:
    # ARRANGE: enable real feature detection; upload persists the exact background task payload.
    # REAL PRODUCTION: the image admin saves an editor's choice, then Wagtail executes the captured task.
    # EXTERNAL SEAM: Willow's face detector returns fixed face coordinates; file loading/crop logic stay real.
    # OBSERVE: the persisted focal point, whose pixels determine the customer's rendered crop.
    with (
        override_settings(WAGTAILIMAGES_FEATURE_DETECTION_ENABLED=True),
        mock.patch.object(WillowImage, "detect_faces", lambda _image: [(5, 5, 25, 25)], create=True),
    ):
        identity, _path, editor = arrange()
        task = DBTaskResult.objects.get(task_path="wagtail.images.tasks.set_image_focal_point_task")
        arguments = task.args_kwargs["args"]

        def edit() -> None:
            client = Client()
            client.force_login(editor)
            response = client.post(
                f"/admin/images/{identity}/",
                {
                    "title": "Editor-chosen crop",
                    "focal_point_x": 40,
                    "focal_point_y": 40,
                    "focal_point_width": 20,
                    "focal_point_height": 20,
                },
            )
            assert response.status_code == 302, response.status_code

        yield SupersededSnapshot(
            name="Wagtail automatic focal point after manual edit",
            take_snapshot=lambda: arguments,
            supersede=edit,
            run_with_snapshot=lambda args: set_image_focal_point_task.call(*args),
            observe=lambda: (
                get_image_model()
                .objects.filter(pk=identity)
                .values_list("focal_point_x", "focal_point_y", "focal_point_width", "focal_point_height")
                .get()
            ),
        )


@contextmanager
def focal_point_replay(arrange: Callable[[], Any]) -> Iterator[ReplaySafeEffect]:
    # ARRANGE: a real newly uploaded image with automatic feature detection enabled.
    # REAL PRODUCTION: Wagtail's actual image task, with the persisted dispatch arguments.
    # EXTERNAL SEAM: Willow face detection counts calls and returns stable coordinates.
    # OBSERVE: the image's persisted focal point, independently from the detector count.
    calls: list[int] = []

    def faces(_image: WillowImage) -> list[tuple[int, int, int, int]]:
        calls.append(1)
        return [(5, 5, 25, 25)]

    with (
        override_settings(WAGTAILIMAGES_FEATURE_DETECTION_ENABLED=True),
        mock.patch.object(WillowImage, "detect_faces", faces, create=True),
    ):
        identity, _path, _editor = arrange()
        task = DBTaskResult.objects.get(task_path="wagtail.images.tasks.set_image_focal_point_task")
        yield ReplaySafeEffect(
            name="Wagtail automatic crop replay",
            prepare=lambda: task.args_kwargs["args"],
            execute=lambda args: set_image_focal_point_task.call(*args),
            observe=lambda _args: (
                get_image_model()
                .objects.filter(pk=identity)
                .values_list("focal_point_x", "focal_point_y", "focal_point_width", "focal_point_height")
                .get()
            ),
            execution_count_for=lambda _args: len(calls),
        )
