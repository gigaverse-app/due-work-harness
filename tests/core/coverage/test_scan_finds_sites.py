"""
Site discovery: every handoff is attributed to the exact function that makes it.

Each kind is shown finding its sites and ignoring look-alikes: a ``.delay`` on
something that is not a registered task, a ``queue.enqueue`` of a function that
is not a workflow. Each way of reaching a handoff indirectly (an import alias,
a local name, a re-export, a registration by assignment) is shown still found.
"""

from pathlib import Path

import pytest

from due_work_harness.coverage import production_sites, scan

from .builders import write_project

ON_COMMIT = "from django.db import transaction\n\ndef place():\n    transaction.on_commit(print)\n"


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


@pytest.mark.parametrize(
    "body",
    [
        "from django.db.transaction import on_commit as later\n\ndef place():\n    later(print)\n",
        "from django.db import transaction as tx\n\ndef place():\n    tx.on_commit(print)\n",
        "import django.db.transaction\n\ndef place():\n    django.db.transaction.on_commit(print)\n",
        "from asgiref.sync import sync_to_async\nfrom django.db.transaction import on_commit\n\n"
        "async def place():\n    await sync_to_async(on_commit)(print)\n",
        "import functools\nfrom django.db import transaction\n\n"
        "def place():\n    functools.partial(transaction.on_commit, print)()\n",
        "from django.db import transaction\n\ndef place(callbacks):\n    callbacks.append(transaction.on_commit)\n",
        "from django.db import transaction\n\ndef place():\n    hook = transaction.on_commit\n    hook(print)\n",
        "from django.db import transaction\n\ndef place(hook=transaction.on_commit):\n    hook(print)\n",
        "from django.db import transaction\n\nlater = transaction.on_commit\n\ndef place():\n    later(print)\n",
        "def place():\n    from django.db.transaction import on_commit as later\n    later(print)\n",
    ],
    ids=[
        "import-alias",
        "module-alias",
        "dotted-import",
        "sync-to-async",
        "partial",
        "passed-along",
        "local-name",
        "parameter-default",
        "module-level-name",
        "function-level-import",
    ],
)
def test_a_handoff_reached_indirectly_is_one_site_in_its_function(tmp_path: Path, body: str) -> None:
    assert _sites(tmp_path, {"shop/orders.py": body}) == {"shop.orders.place": ["django"]}


def test_a_handoff_bound_to_self_is_a_site_where_it_is_used(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/orders.py": """
                from django.db import transaction

                class OrderService:
                    def place(self):
                        self.after_commit(print)

                    def __init__(self, after_commit=transaction.on_commit):
                        self.after_commit = after_commit
            """
        },
    )
    assert sites == {"shop.orders.OrderService.place": ["django"]}


def test_a_nested_function_or_lambda_counts_for_its_outermost_function(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/orders.py": """
                from django.db import transaction

                class OrderService:
                    def place(self):
                        def later():
                            transaction.on_commit(print)
                        callback = lambda: transaction.on_commit(print)
                        return later, callback
            """
        },
    )
    assert sites == {"shop.orders.OrderService.place": ["django", "django"]}


def test_a_look_alike_name_is_not_a_site(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/orders.py": """
                from django.db import transaction

                def place(on_commit):
                    on_commit(print)
                    transaction.atomic()
            """
        },
    )
    assert sites == {}


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
    )
    assert sites == {"shop.orders.place": ["celery", "celery", "celery", "celery"]}


def test_a_task_registered_by_assignment_is_a_task(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tasks.py": """
                from celery import shared_task
                from shop.worker import app

                def _send(order_id): ...
                def _refund(order_id): ...

                send = shared_task(_send)
                refund = app.task(name="shop.refund")(_refund)
            """,
            "shop/worker.py": "from celery import Celery\n\napp = Celery('shop')\n",
            "shop/orders.py": """
                from shop.tasks import refund, send

                def place():
                    send.delay(1)
                    refund.apply_async((1,))
            """,
        },
    )
    assert sites == {"shop.orders.place": ["celery", "celery"]}


def test_a_task_reached_through_a_package_re_export_or_a_local_name_is_a_task(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/__init__.py": "from .tasks import send_receipt\n",
            "shop/tasks.py": "from celery import shared_task\n\n@shared_task\ndef send_receipt(order_id): ...\n",
            "shop/orders.py": """
                import shop
                from shop import send_receipt

                def place():
                    send_receipt.delay(1)
                    task = shop.send_receipt
                    task.delay(2)
            """,
        },
    )
    assert sites == {"shop.orders.place": ["celery", "celery"]}


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
    )
    assert sites == {"shop.flows.place": ["dbos", "dbos"]}


def test_kinds_come_from_what_production_imports_not_what_is_installed(tmp_path: Path) -> None:
    config = write_project(tmp_path, {"shop/orders.py": ON_COMMIT})
    assert scan(config).kinds == ["django"]


def test_a_framework_imported_through_another_project_module_enables_its_kind(tmp_path: Path) -> None:
    report = scan(
        write_project(
            tmp_path,
            {
                "infra/worker.py": "from celery import Celery\n\napp = Celery('shop')\n",
                "shop/orders.py": """
                    from infra.worker import app

                    def place():
                        app.send_task("shop.tasks.send_receipt")
                """,
            },
        )
    )
    assert report.kinds == ["celery"]
    assert {name: [site.kind for site in sites] for name, sites in report.sites.items()} == {
        "shop.orders.place": ["celery"]
    }


def test_configured_sites_add_kinds_and_never_remove_a_detected_one(tmp_path: Path) -> None:
    report = scan(write_project(tmp_path, {"shop/orders.py": ON_COMMIT}, sites='["dbos"]'))
    assert report.kinds == ["django", "dbos"]
    assert list(report.sites) == ["shop.orders.place"]


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
        extra='bridges = { "shop.shared.after_commit" = 1 }\nbridge-methods = ["request_progress"]',
    )
    assert sites == {"shop.shared.after_commit": ["django"], "shop.orders.place": ["bridge", "bridge"]}


def test_test_code_is_never_production(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tests/test_orders.py": ON_COMMIT,
            "shop/conftest.py": ON_COMMIT,
            "shop/orders_test.py": ON_COMMIT,
            "other/orders.py": ON_COMMIT,
        },
    )
    assert sites == {}
