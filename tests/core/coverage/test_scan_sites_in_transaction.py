"""
Handoffs made inside ``transaction.atomic()`` are reported; the ones that wait for the commit are not.

Each shape that puts a handoff in a transaction is shown found, and each that
only looks like one is shown left alone: ``on_commit``, Celery's ``*_on_commit``
variants, and a callback defined inside the block, which runs after it.
"""

from pathlib import Path

import pytest

from due_work_harness.coverage import sites_in_transaction
from due_work_harness.coverage.cli import main

from .builders import write_project

TASKS = """
    from celery import shared_task

    @shared_task
    def notify(order_id): ...
"""


def _found(root: Path, source: str) -> dict[str, list[int]]:
    files = {"shop/tasks.py": TASKS, "shop/orders.py": source}
    found = sites_in_transaction(write_project(root, files))
    return {name: [site.line for site in sites] for name, sites in found.items()}


def test_a_publish_inside_the_block_is_reported_with_its_line(tmp_path: Path) -> None:
    found = _found(
        tmp_path,
        """
            from django.db import transaction
            from shop.tasks import notify

            def place(order):
                with transaction.atomic():
                    order.save()
                    notify.delay(order.pk)
        """,
    )
    assert found == {"shop.orders.place": [8]}


@pytest.mark.parametrize(
    "header",
    [
        "from django.db import transaction\n\ndef place(order):\n    with transaction.atomic(durable=True):",
        "from django.db.transaction import atomic\n\ndef place(order):\n    with atomic():",
        "from django.db import transaction as tx\n\ndef place(order):\n    with tx.atomic(using='replica'):",
    ],
    ids=["durable", "imported-name", "aliased-module"],
)
def test_every_spelling_of_the_block_counts(tmp_path: Path, header: str) -> None:
    found = _found(tmp_path, f"from shop.tasks import notify\n{header}\n        notify.delay(order.pk)\n")
    assert list(found) == ["shop.orders.place"]


def test_a_decorated_function_is_inside_its_transaction_throughout(tmp_path: Path) -> None:
    found = _found(
        tmp_path,
        """
            from django.db import transaction
            from shop.tasks import notify

            @transaction.atomic
            def place(order):
                notify.apply_async((order.pk,))
        """,
    )
    assert found == {"shop.orders.place": [7]}


def test_a_publish_after_the_block_is_not_inside_it(tmp_path: Path) -> None:
    found = _found(
        tmp_path,
        """
            from django.db import transaction
            from shop.tasks import notify

            def place(order):
                with transaction.atomic():
                    order.save()
                notify.delay(order.pk)
        """,
    )
    assert found == {}


@pytest.mark.parametrize(
    "publish",
    [
        "transaction.on_commit(lambda: notify.delay(order.pk))",
        "notify.delay_on_commit(order.pk)",
        "notify.apply_async_on_commit((order.pk,))",
        "transaction.on_commit(send)",
    ],
    ids=["on_commit-lambda", "delay_on_commit", "apply_async_on_commit", "on_commit-named-callback"],
)
def test_work_that_waits_for_the_commit_is_not_reported(tmp_path: Path, publish: str) -> None:
    found = _found(
        tmp_path,
        f"""
            from django.db import transaction
            from shop.tasks import notify

            def place(order):
                with transaction.atomic():
                    def send():
                        notify.delay(order.pk)
                    {publish}
        """,
    )
    assert found == {}


def test_a_look_alike_delay_on_a_non_task_is_not_reported(tmp_path: Path) -> None:
    found = _found(
        tmp_path,
        """
            from django.db import transaction

            def place(order, cache):
                with transaction.atomic():
                    cache.delay(order.pk)
        """,
    )
    assert found == {}


def test_the_command_lists_them_and_fails_only_when_there_are_some(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_project(
        tmp_path,
        {
            "shop/tasks.py": TASKS,
            "shop/orders.py": (
                "from django.db import transaction\nfrom shop.tasks import notify\n\n"
                "def place(order):\n    with transaction.atomic():\n        notify.delay(order.pk)\n"
            ),
        },
    )

    assert main(["in-transaction", "--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "shop/orders.py:6\tcelery\tshop.orders.place" in out.replace("\\", "/")
    assert "1 handoff(s) inside transaction.atomic()" in out

    (tmp_path / "shop" / "orders.py").write_text(
        "from django.db import transaction\nfrom shop.tasks import notify\n\n"
        "def place(order):\n    with transaction.atomic():\n        pass\n    notify.delay(order.pk)\n",
        encoding="utf-8",
    )
    assert main(["in-transaction", "--root", str(tmp_path)]) == 0
    assert "no handoff inside transaction.atomic()" in capsys.readouterr().out


@pytest.mark.parametrize(
    "deferral",
    [
        "transaction.on_commit(partial(notify.delay, order.pk))",
        "transaction.on_commit(notify.s(order.pk).delay)",
        "send = notify.delay\n        transaction.on_commit(send)",
    ],
    ids=["partial", "signature", "alias"],
)
def test_a_handoff_passed_to_on_commit_runs_after_the_block(tmp_path: Path, deferral: str) -> None:
    found = _found(
        tmp_path,
        f"""
            from functools import partial
            from django.db import transaction
            from shop.tasks import notify

            def place(order):
                with transaction.atomic():
                    {deferral}
        """,
    )
    assert found == {}


def test_a_handoff_passed_to_a_configured_bridge_runs_after_the_block(tmp_path: Path) -> None:
    files = {
        "shop/tasks.py": TASKS,
        "shop/orders.py": """
            from django.db import transaction
            from shop.tasks import notify

            class Orders:
                def place(self, order):
                    with transaction.atomic():
                        self.after_commit(notify.delay, order.pk)
        """,
    }
    found = sites_in_transaction(write_project(tmp_path, files, extra='bridge-methods = ["after_commit"]'))
    assert found == {}


def test_a_queue_that_can_be_a_table_in_the_callers_database_is_not_reported(tmp_path: Path) -> None:
    files = {
        "shop/tasks.py": "from django.tasks import task\n\n@task\ndef notify(order_id): ...\n",
        "shop/orders.py": """
            from django.db import transaction
            from shop.tasks import notify

            def place(order):
                with transaction.atomic():
                    notify.enqueue(order.pk)
        """,
    }
    assert sites_in_transaction(write_project(tmp_path, files)) == {}


def test_a_broker_that_cannot_join_the_transaction_is_reported(tmp_path: Path) -> None:
    files = {
        "shop/orders.py": """
            import django_rq
            from django.db import transaction

            def place(order, send):
                with transaction.atomic():
                    django_rq.enqueue(send, order.pk)
        """
    }
    found = sites_in_transaction(write_project(tmp_path, files))
    assert list(found) == ["shop.orders.place"]
