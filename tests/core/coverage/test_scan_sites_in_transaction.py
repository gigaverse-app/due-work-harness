"""
Handoffs made inside ``transaction.atomic()`` are reported; the ones that wait for the commit are not.

Each shape that puts a handoff in a transaction is shown found, and each that only looks like one is
shown left alone. The groups below follow the judgement in ``_Sites._in_transaction``:

* the block: every spelling of ``atomic``, and a decorated function;
* deferrals: ``on_commit``, what is passed to it, a configured bridge, and ``*_on_commit`` methods;
* queues that commit with the block (a table in the caller's database) versus brokers that cannot.
"""

from pathlib import Path
from textwrap import dedent, indent

import pytest

from pytest_obligation.coverage import sites_in_transaction
from pytest_obligation.coverage.cli import main

from .builders import write_project

TASKS = """
    from celery import shared_task

    @shared_task
    def notify(order_id): ...
"""

IMPORTS = """
    from functools import partial
    from django.db import transaction
    from shop.tasks import notify
"""


def _found(root: Path, orders: str, *, tasks: str = TASKS, extra: str = "") -> dict[str, list[int]]:
    """The functions of ``shop/orders.py`` with a handoff in a transaction, and the lines of those handoffs."""
    files = {"shop/tasks.py": tasks, "shop/orders.py": orders}
    found = sites_in_transaction(write_project(root, files, extra=extra))
    return {name: [site.line for site in sites] for name, sites in found.items()}


def _in_the_block(*statements: str) -> str:
    """``shop/orders.py`` whose ``place`` runs ``statements`` inside ``with transaction.atomic():``."""
    body = indent(dedent("\n".join(statements)), " " * 8)
    return f"{dedent(IMPORTS)}\ndef place(order):\n    with transaction.atomic():\n{body}\n"


def test_a_publish_inside_the_block_is_reported_with_its_line(tmp_path: Path) -> None:
    found = _found(tmp_path, _in_the_block("order.save()", "notify.delay(order.pk)"))

    # Imports take lines 1-4, a blank line 5, then: def (6), with (7), save (8), delay (9).
    assert found == {"shop.orders.place": [9]}


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
    decorated = """
        @transaction.atomic
        def place(order):
            notify.apply_async((order.pk,))
    """
    assert list(_found(tmp_path, dedent(IMPORTS) + dedent(decorated))) == ["shop.orders.place"]


def test_a_publish_after_the_block_is_not_inside_it(tmp_path: Path) -> None:
    orders = f"{dedent(IMPORTS)}\ndef place(order):\n    with transaction.atomic():\n        order.save()\n    notify.delay(order.pk)\n"

    assert _found(tmp_path, orders) == {}


@pytest.mark.parametrize(
    "deferral",
    [
        "transaction.on_commit(lambda: notify.delay(order.pk))",
        "notify.delay_on_commit(order.pk)",
        "notify.apply_async_on_commit((order.pk,))",
        "transaction.on_commit(partial(notify.delay, order.pk))",
        "transaction.on_commit(notify.s(order.pk).delay)",
        "send = notify.delay\ntransaction.on_commit(send)",
        "def send():\n    notify.delay(order.pk)\ntransaction.on_commit(send)",
    ],
    ids=["lambda", "delay_on_commit", "apply_async_on_commit", "partial", "signature", "alias", "named-callback"],
)
def test_work_that_waits_for_the_commit_is_not_reported(tmp_path: Path, deferral: str) -> None:
    assert _found(tmp_path, _in_the_block(deferral)) == {}


def test_a_handoff_passed_to_a_configured_bridge_runs_after_the_block(tmp_path: Path) -> None:
    orders = """
        class Orders:
            def place(self, order):
                with transaction.atomic():
                    self.after_commit(notify.delay, order.pk)
    """
    found = _found(tmp_path, dedent(IMPORTS) + dedent(orders), extra='bridge-methods = ["after_commit"]')

    assert found == {}


def test_a_look_alike_delay_on_a_non_task_is_not_reported(tmp_path: Path) -> None:
    orders = "from django.db import transaction\n\ndef place(order, cache):\n    with transaction.atomic():\n        cache.delay(order.pk)\n"

    assert _found(tmp_path, orders) == {}


def test_a_queue_that_can_be_a_table_in_the_callers_database_is_not_reported(tmp_path: Path) -> None:
    tasks = "from django.tasks import task\n\n@task\ndef notify(order_id): ...\n"

    assert _found(tmp_path, _in_the_block("notify.enqueue(order.pk)"), tasks=tasks) == {}


def test_a_broker_that_cannot_join_the_transaction_is_reported(tmp_path: Path) -> None:
    orders = (
        "import django_rq\nfrom django.db import transaction\n\n"
        "def place(order, send):\n    with transaction.atomic():\n        django_rq.enqueue(send, order.pk)\n"
    )

    assert list(_found(tmp_path, orders)) == ["shop.orders.place"]


def test_the_command_lists_them_and_fails_only_when_there_are_some(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_project(tmp_path, {"shop/tasks.py": TASKS, "shop/orders.py": _in_the_block("notify.delay(order.pk)")})

    assert main(["in-transaction", "--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "shop/orders.py:8\tcelery\tshop.orders.place" in out.replace("\\", "/")
    assert "1 handoff(s) inside transaction.atomic()" in out

    (tmp_path / "shop" / "orders.py").write_text(
        f"{dedent(IMPORTS)}\ndef place(order):\n    with transaction.atomic():\n        pass\n    notify.delay(order.pk)\n",
        encoding="utf-8",
    )
    assert main(["in-transaction", "--root", str(tmp_path)]) == 0
    assert "no handoff inside transaction.atomic()" in capsys.readouterr().out
