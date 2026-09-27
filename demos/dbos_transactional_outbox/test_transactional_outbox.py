"""
The harness against DBOS's transactional-outbox demo, unmodified. No Django here.

Upstream: dbos-demo-apps @ 45a68c2 (``python/transactional-outbox/transactional_enqueue.py``),
dbos 3.1.0. Placing an order inserts it and, in the same transaction, enqueues
``send_notification_workflow``, which sends a notification (a DBOS step) and
marks the order ``SENT``. The demo's docstring: the workflow "runs exactly once
for every committed order, recovering automatically if this process crashes".

The worker is DBOS's own executor, which a test cannot reach in-process, so the
deaths are real: ``run_demo_process.py`` runs the demo in a child process that
``os._exit``\\ s at a named point, and recovery is the demo's ``main()`` starting
again. The harness owns the verdict (:mod:`due_work_harness.process_histories`).
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from due_work_harness import LossIsAbsorbedElsewhere
from due_work_harness.integrations.dbos import restart_until
from due_work_harness.process_histories import ProcessHistory, assert_process_deaths_converge

HERE = Path(__file__).resolve().parent
DEMO_DIR = HERE.parent / ".upstream" / "dbos-demo-apps" / "python" / "transactional-outbox"
DATABASE = os.environ.get("DBOS_DEMO_DATABASE", "dbos_outbox_demo")
_PG = {
    key: os.environ.get(f"PG{key.upper()}", default)
    for key, default in [("user", "postgres"), ("password", "postgres"), ("host", "localhost"), ("port", "5432")]
}
_URL = f"postgresql://{_PG['user']}:{_PG['password']}@{_PG['host']}:{_PG['port']}"
os.environ["DBOS_DATABASE_URL"] = f"{_URL.replace('postgresql://', 'postgresql+psycopg://')}/{DATABASE}"

with psycopg.connect(f"{_URL}/postgres", autocommit=True) as admin:
    admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {DATABASE}")

sys.path.insert(0, str(DEMO_DIR))
import transactional_enqueue as demo  # noqa: E402

INBOX = HERE / ".inbox.txt"
_customers: dict[int, str] = {}


def _status(order_id: int) -> str | None:
    with psycopg.connect(f"{_URL}/{DATABASE}") as db:
        row = db.execute("SELECT notification_status FROM orders WHERE order_id = %s", [order_id]).fetchone()
    return row[0] if row else None


def observe(order_id: int) -> tuple[str | None, int]:
    """OBSERVE: the order's notification status, and how many notifications the customer received."""
    received = INBOX.read_text().splitlines().count(_customers[order_id]) if INBOX.exists() else 0
    return _status(order_id), received


def place_order_in_a_child_process(death_point: str | None) -> tuple[int, int]:
    """FAULT INJECTION: the demo places one order in its own process, dying at ``death_point``."""
    customer = f"customer-{uuid4().hex[:8]}"
    env = {**os.environ, "DEMO_DIR": str(DEMO_DIR), "SENDS_FILE": str(INBOX), "CUSTOMER": customer}
    if death_point is not None:
        env["DIE_AT"] = death_point
    child = subprocess.run(
        [sys.executable, str(HERE / "run_demo_process.py")], env=env, capture_output=True, text=True, timeout=120
    )
    orders = [line for line in child.stdout.splitlines() if line.startswith("ORDER ")]
    assert orders, f"the demo never placed an order (exit {child.returncode}):\n{child.stderr[-2000:]}"
    order_id = int(orders[0].split()[1])
    _customers[order_id] = customer
    return order_id, child.returncode


def restart(order_id: int) -> None:
    """REAL PRODUCTION: the demo's own main() starting again, until this order is settled."""

    def send(_seconds: float) -> None:
        # EXTERNAL SEAM: the same inbox the dead process wrote to.
        with INBOX.open("a") as inbox:
            inbox.write(f"{_customers[order_id]}\n")

    previous = demo.time, demo.uvicorn
    demo.time, demo.uvicorn = SimpleNamespace(sleep=send), SimpleNamespace(run=lambda *_a, **_k: None)
    try:
        restart_until(demo.main, lambda: _status(order_id) == "SENT")
    finally:
        demo.time, demo.uvicorn = previous


PLACE_ORDER = ProcessHistory(
    name="place order",
    initial=(None, 0),
    run=place_order_in_a_child_process,
    observe=observe,
    recover=restart,
    death_points=("after_order", "before_send"),
)


def test_placing_an_order_survives_a_death_before_the_notification() -> None:
    assert_process_deaths_converge(PLACE_ORDER)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING: a death after the notification is sent and before DBOS records the step makes recovery run "
        "the step again, so the customer is notified twice. DBOS resumes from the last completed step, as "
        "documented; the workflow runs once, but its external effect is at-least-once."
    ),
)
def test_placing_an_order_survives_a_death_after_the_notification() -> None:
    history = ProcessHistory(**{**PLACE_ORDER.__dict__, "death_points": ("after_send",)})
    assert_process_deaths_converge(history)


def test_an_order_whose_process_died_before_its_workflow_ran_is_still_notified() -> None:
    """The harness's exemption probe, unmodified: lose the execution, require production to absorb it."""

    def strand() -> int:
        order_id, status = place_order_in_a_child_process("after_order")
        assert status == 1
        return order_id

    LossIsAbsorbedElsewhere(strand=strand, observe=lambda order_id: _status(order_id) == "SENT", absorb=restart)()
