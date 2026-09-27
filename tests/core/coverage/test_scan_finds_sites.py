"""
Site discovery: every handoff is attributed to the exact function that makes it.

Each kind is shown finding its sites and ignoring look-alikes: a ``.delay`` on
something that is not a registered task, a ``queue.enqueue`` of a function that
is not a workflow.
"""

from pathlib import Path

from due_work_harness.coverage import production_sites

from .builders import write_project


def _sites(root: Path, files: dict[str, str], **kwargs: str) -> dict[str, list[str]]:
    found = production_sites(write_project(root, files, **kwargs))
    return {name: [site.kind for site in sites] for name, sites in found.items()}


def test_on_commit_is_attributed_per_callable_not_per_file(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/orders.py": """
                from django.db import transaction

                class OrderService:
                    def place(self):
                        transaction.on_commit(lambda: None)

                    def cancel(self):
                        transaction.on_commit(lambda: None)
                        transaction.on_commit(lambda: None)

                transaction.on_commit(lambda: None)
            """
        },
    )
    assert sites == {
        "shop.orders.OrderService.place": ["django"],
        "shop.orders.OrderService.cancel": ["django", "django"],
        "shop.orders.<module>": ["django"],
    }


def test_an_on_commit_wrapped_for_async_code_is_a_site(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/orders.py": """
                from asgiref.sync import sync_to_async
                from django.db.transaction import on_commit

                async def place():
                    await sync_to_async(on_commit)(lambda: None)
            """
        },
    )
    assert sites == {"shop.orders.place": ["django"]}


def test_celery_publications_count_only_on_registered_tasks(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tasks.py": """
                from celery import shared_task

                @shared_task
                def send_receipt(order_id): ...

                def not_a_task(order_id): ...
            """,
            "shop/orders.py": """
                from celery import current_app
                from shop.tasks import not_a_task, send_receipt
                from . import tasks

                def place(cache):
                    send_receipt.delay(1)
                    tasks.send_receipt.apply_async((1,), countdown=5)
                    send_receipt.s(1).apply_async()
                    current_app.send_task("shop.tasks.send_receipt", args=[1])
                    cache.delay(1)
                    not_a_task.delay(1)
            """,
        },
        sites='["celery"]',
    )
    assert sites == {"shop.orders.place": ["celery", "celery", "celery", "celery"]}


def test_procrastinate_deferrals_count_through_configure(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tasks.py": """
                from procrastinate.contrib.django import app

                @app.task(queue="index")
                def index_book(book_id): ...
            """,
            "shop/views.py": """
                from shop import tasks

                def create(book_id):
                    tasks.index_book.defer(book_id=book_id)

                async def create_async(book_id):
                    await tasks.index_book.configure(lock=str(book_id)).defer_async(book_id=book_id)
            """,
        },
        sites='["procrastinate"]',
    )
    assert sites == {"shop.views.create": ["procrastinate"], "shop.views.create_async": ["procrastinate"]}


def test_dbos_starts_and_enqueues_count_for_workflows_only(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/flows.py": """
                from dbos import DBOS, Queue

                queue = Queue("notifications")

                @DBOS.workflow()
                def notify(order_id): ...

                def helper(order_id): ...

                def place(order_id):
                    DBOS.start_workflow(notify, order_id)
                    queue.enqueue(notify, order_id)
                    queue.enqueue(helper, order_id)
            """
        },
        sites='["dbos"]',
    )
    assert sites == {"shop.flows.place": ["dbos", "dbos"]}


def test_a_kind_that_is_not_enabled_finds_nothing(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {"shop/orders.py": "from django.db import transaction\n\ndef place():\n    transaction.on_commit(print)\n"},
        sites='["celery"]',
    )
    assert sites == {}


def test_a_call_to_a_forwarding_helper_is_a_site_in_its_caller(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/shared.py": """
                from django.db import transaction

                def after_commit(callback):
                    transaction.on_commit(callback)
            """,
            "shop/orders.py": """
                from shop.shared import after_commit

                def place(lease):
                    after_commit(print)
                    lease.request_progress(1)
            """,
        },
        sites='["django"]',
        extra='bridges = { "shop.shared.after_commit" = 1 }\nbridge-methods = ["request_progress"]',
    )
    assert sites == {"shop.shared.after_commit": ["django"], "shop.orders.place": ["bridge", "bridge"]}


def test_test_code_is_never_production(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tests/test_orders.py": "from django.db import transaction\n\ndef t():\n    transaction.on_commit(print)\n",
            "shop/conftest.py": "from django.db import transaction\n\ndef f():\n    transaction.on_commit(print)\n",
            "other/orders.py": "from django.db import transaction\n\ndef f():\n    transaction.on_commit(print)\n",
        },
    )
    assert sites == {}
