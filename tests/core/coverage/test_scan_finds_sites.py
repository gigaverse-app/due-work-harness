"""
Site discovery: every handoff is attributed to the exact function that makes it.

Each kind is shown finding its sites and ignoring look-alikes: a ``.delay`` on
something that is not a registered task, a ``queue.enqueue`` of a function that
is not a workflow. Each way of reaching a handoff indirectly (an import alias,
a local name, a re-export, a registration by assignment) is shown still found.
"""

from pathlib import Path

import pytest

from pytest_obligation.coverage import production_sites, scan

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


def test_celerys_on_commit_publications_are_sites(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tasks.py": "from celery import shared_task\n\n@shared_task\ndef send_receipt(order_id): ...\n",
            "shop/orders.py": """
                from shop.tasks import send_receipt

                def place():
                    send_receipt.delay_on_commit(1)
                    send_receipt.apply_async_on_commit((1,))
            """,
        },
    )
    assert sites == {"shop.orders.place": ["celery", "celery"]}


def test_dramatiq_sends_count_only_on_actors(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tasks.py": """
                import dramatiq
                from dramatiq import actor

                @actor
                def index_book(book_id): ...

                @dramatiq.actor(queue_name="mail")
                def send_receipt(order_id): ...
            """,
            "shop/orders.py": """
                from django.dispatch import Signal
                from shop.tasks import index_book, send_receipt

                order_placed = Signal()

                def place(order_id):
                    index_book.send(order_id)
                    send_receipt.send_with_options(args=(order_id,), delay=1000)
                    order_placed.send(sender=None, order_id=order_id)
            """,
        },
    )
    assert sites == {"shop.orders.place": ["dramatiq", "dramatiq"]}


def test_rq_enqueues_count_on_rq_objects_only(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/jobs.py": """
                import django_rq
                from datetime import timedelta
                from django_rq import enqueue
                from rq import Queue
                from rq.decorators import job

                @job("default")
                def reindex(pk): ...

                def rebuild(pk): ...

                class Job:
                    @classmethod
                    def enqueue(cls, func): ...

                class Scheduler:
                    def __init__(self):
                        self.queue = django_rq.get_queue("low")

                    def later(self, pk):
                        self.queue.enqueue_in(timedelta(minutes=5), rebuild, pk)

                def place(pk, connection, planner):
                    django_rq.enqueue(rebuild, pk)
                    enqueue(rebuild, pk)
                    queue = django_rq.get_queue("default")
                    queue.enqueue(rebuild, pk)
                    Queue("high", connection=connection).enqueue_call(func=rebuild, args=(pk,))
                    reindex.delay(pk)
                    Job.enqueue(rebuild)
                    planner.enqueue(rebuild)
            """
        },
    )
    assert sites == {"shop.jobs.Scheduler.later": ["rq"], "shop.jobs.place": ["rq"] * 5}


def test_django_tasks_count_only_for_djangos_task_decorator(tmp_path: Path) -> None:
    sites = _sites(
        tmp_path,
        {
            "shop/tasks.py": """
                from celery import Celery
                from django.tasks import task

                app = Celery("shop")

                @task(priority=2)
                def send_receipt(order_id): ...

                @app.task
                def reindex(order_id): ...
            """,
            "shop/orders.py": """
                from shop.tasks import reindex, send_receipt

                async def place(order_id):
                    send_receipt.enqueue(order_id)
                    await send_receipt.aenqueue(order_id)
                    reindex.enqueue(order_id)
            """,
        },
    )
    assert sites == {"shop.orders.place": ["django-tasks", "django-tasks"]}


def test_django_alone_does_not_enable_django_tasks(tmp_path: Path) -> None:
    assert scan(write_project(tmp_path, {"shop/orders.py": ON_COMMIT})).kinds == ["django"]


def test_a_file_this_python_cannot_parse_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    report = scan(write_project(tmp_path, {"shop/orders.py": ON_COMMIT, "shop/broken.py": "def broken(:\n"}))
    assert list(report.sites) == ["shop.orders.place"]
    (problem,) = [problem for problem in report.problems if "broken.py" in problem]
    assert problem.startswith("shop/broken.py:1 cannot be parsed by Python 3.")
    assert problem.endswith("run the check with the project's Python or newer")


def test_a_source_root_outside_the_project_is_scanned_and_named_from_the_project(tmp_path: Path) -> None:
    project = tmp_path / "adopter"
    project.mkdir()
    upstream = tmp_path / "upstream" / "shop"
    upstream.mkdir(parents=True)
    (upstream / "__init__.py").write_text("", encoding="utf-8")
    (upstream / "orders.py").write_text(ON_COMMIT, encoding="utf-8")
    config = write_project(project, {}, extra='source-roots = ["../upstream"]')
    (site,) = production_sites(config)["shop.orders.place"]
    assert site.path == "../upstream/shop/orders.py"
