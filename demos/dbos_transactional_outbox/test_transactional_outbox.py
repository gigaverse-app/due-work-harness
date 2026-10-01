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
again. The harness owns the verdict (:mod:`pytest_obligation.process_histories`).

Where DBOS has a capability, the contract claims it and the harness proves it
against DBOS itself, through ``pytest_obligation.integrations.dbos``: its
workflow garbage collection (profile D), and the demo's notification step's
retry budget. Each of those proofs launches the demo through its own
``main()``, with only its HTTP server and its queue listener held back.
"""

import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from uuid import uuid4

import psycopg
import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from psycopg import sql

from pytest_obligation import (
    AdmissionAtomicity,
    Adoption,
    BoundedRetry,
    Claim,
    Decline,
    ExtraProof,
    KnownGap,
    LossIsAbsorbedElsewhere,
    NotApplicable,
    ObligationContract,
    Profile,
    ReplaySafeEffect,
    Retention,
    due_work_contract_suite,
)
from pytest_obligation.host import Host, hosted
from pytest_obligation.integrations.dbos import OUTSTANDING, launched, restart_until, wait_until, workflow_status
from pytest_obligation.integrations.dbos import retention as dbos_retention
from pytest_obligation.process_histories import ProcessHistory, assert_process_deaths_converge, fault_environment

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
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DATABASE)))
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))

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
    env = {
        **os.environ,
        "DEMO_DIR": str(DEMO_DIR),
        "SENDS_FILE": str(INBOX),
        "CUSTOMER": customer,
        **fault_environment(death_point),
    }
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

    with (
        mock.patch.object(demo, "time", SimpleNamespace(sleep=send)),
        mock.patch.object(demo, "uvicorn", SimpleNamespace(run=lambda *_a, **_k: None)),
    ):
        restart_until(demo.main, lambda: _status(order_id) == "SENT")


def place_order(*death_points: str) -> ProcessHistory[int, tuple[str | None, int]]:
    """Placing one order, the process dying at each of ``death_points``, recovered by the demo's own restart."""
    return ProcessHistory(
        name="place order",
        initial=(None, 0),
        run=place_order_in_a_child_process,
        observe=observe,
        recover=restart,
        death_points=death_points,
    )


# ARRANGE: the demo places an order in a child process that dies after the order commits, or before sending.
# REAL PRODUCTION: the demo's own main() restarts and DBOS recovers the workflow (restart).
# EXTERNAL SEAM: the notification lands in the same inbox the dead process wrote to.
# OBSERVE: the order's notification status, and how many notifications the customer received.
DEATHS_BEFORE_THE_NOTIFICATION = place_order("after_order", "before_send")


def a_death_after_the_notification() -> None:
    # ARRANGE: the child process dies after the notification is sent, before DBOS records the step.
    # REAL PRODUCTION: the demo's main() restarts and DBOS reruns the step it has no record of.
    # EXTERNAL SEAM: the same inbox.
    # OBSERVE: the customer's notification count.
    assert_process_deaths_converge(place_order("after_send"))


def _strand() -> int:
    order_id, status = place_order_in_a_child_process("after_order")
    if status != 1:
        raise RuntimeError(f"the child process was to die after committing the order; it exited {status}")
    return order_id


#: The harness's own exemption probe, unmodified: lose the execution, require the demo's restart to absorb it.
A_LOST_WORKFLOW_IS_ABSORBED_BY_A_RESTART = LossIsAbsorbedElsewhere(
    strand=_strand, observe=lambda order_id: _status(order_id) == "SENT", absorb=restart
)


#: DBOS's own queue registration, before any proof holds it back.
REGISTER_QUEUE = DBOS.register_queue
#: Notification sends that reached the provider seam, by workflow.
SENDS: Counter[str] = Counter()


@contextmanager
def the_demo_without_its_listener(send: Callable[[float], None]) -> Iterator[None]:
    """The demo's own main(), with its HTTP server and its queue listener held back: enqueued work stays owed."""
    with (
        mock.patch.object(demo, "uvicorn", SimpleNamespace(run=lambda *_a, **_k: None)),
        mock.patch.object(DBOS, "register_queue", lambda *_a, **_k: None),
        # EXTERNAL SEAM: the demo's time.sleep(3) stands in for the notification provider.
        mock.patch.object(demo, "time", SimpleNamespace(sleep=send)),
        launched(demo.main),
    ):
        yield
        # With the listener held back, whatever is still outstanding was arranged by this proof:
        # cancel it through DBOS, so a later launch of the demo does not deliver it.
        for workflow in DBOS.list_workflows(status=list(OUTSTANDING)):
            DBOS.cancel_workflow(workflow.workflow_id)


def order_owing_a_notification() -> str:
    """An order placed through the demo; its notification workflow is enqueued and not yet run."""
    demo.insert_order(f"customer-{uuid4().hex[:8]}", "widget", 1)
    (queued,) = DBOS.list_queued_workflows(queue_name=demo.NOTIFICATION_QUEUE, sort_desc=True, limit=1)
    return queued.workflow_id


def order_notified() -> str:
    """An order placed and notified through the demo's own workflow, now complete."""
    order_id = demo.insert_order(f"customer-{uuid4().hex[:8]}", "widget", 1)
    workflow_id = f"notified-{uuid4()}"
    with SetWorkflowID(workflow_id):
        demo.send_notification_workflow(order_id, "customer", "widget")
    return workflow_id


@contextmanager
def the_demos_retention() -> Iterator[Retention]:
    # ARRANGE: an order still owing its notification, and one notified (the demo's own workflows).
    # REAL PRODUCTION: DBOS's garbage_collect, as its admin endpoint and conductor run it.
    # EXTERNAL SEAM: the notification provider, which answers at once.
    # OBSERVE: whether each workflow still exists, as DBOS records it.
    with the_demo_without_its_listener(lambda _seconds: None):
        yield dbos_retention(make_owed=order_owing_a_notification, make_finished=order_notified)


def _provider_down(_seconds: float) -> None:
    SENDS[DBOS.workflow_id or ""] += 1
    raise ConnectionError("the notification provider is down")


def listen_until_settled(workflow_id: str) -> None:
    """DBOS's own queue listener, as the demo's main() registers it, until this workflow settles."""
    if demo.NOTIFICATION_QUEUE not in {queue.name for queue in DBOS.list_queues()}:
        REGISTER_QUEUE(demo.NOTIFICATION_QUEUE)
    wait_until(
        lambda: workflow_status(workflow_id) not in OUTSTANDING, timeout=30, what="the notification never settled"
    )


@contextmanager
def the_notifications_retry() -> Iterator[BoundedRetry]:
    # ARRANGE: an order whose notification provider is down (order_owing_a_notification).
    # REAL PRODUCTION: DBOS's queue listener running the demo's workflow (listen_until_settled).
    # EXTERNAL SEAM: the provider, which fails and is counted per workflow (SENDS).
    # OBSERVE: the workflow's status, as DBOS records it.
    SENDS.clear()
    with the_demo_without_its_listener(_provider_down):
        yield BoundedRetry(
            name="send_notification_workflow",
            # The demo's step declares no retries: one execution, then an error for good.
            max_executions=1,
            make_failing=order_owing_a_notification,
            due_work=lambda: [workflow.workflow_id for workflow in DBOS.list_workflows(status=list(OUTSTANDING))],
            run_once=listen_until_settled,
            advance_to_due=lambda _workflow_id: None,
            is_terminal=lambda workflow_id: workflow_status(workflow_id) not in OUTSTANDING,
            failure_attempt_count=lambda workflow_id: SENDS[workflow_id],
            observe=workflow_status,
        )


class InterruptedOrder(RuntimeError):
    """An interruption after the real transactional enqueue statement."""


@contextmanager
def order_admission() -> Iterator[AdmissionAtomicity[list[str]]]:
    # ARRANGE: the upstream demo launched with its queue listener stopped.
    # REAL PRODUCTION: insert_order and dbos.enqueue_workflow in its SQLAlchemy transaction.
    # EXTERNAL SEAM: the notification provider records calls; no worker can deliver them yet.
    # OBSERVE: order intent and workflow IDs on the actual writing connection at the checkpoint.
    active: list[sa.Connection] = []
    fault: list[Callable[[], None]] = []
    calls: list[float] = []
    customer = f"admission-{uuid4().hex}"
    observer = sa.create_engine(os.environ["DBOS_DATABASE_URL"])

    def after_statement(
        conn: sa.Connection, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool
    ) -> None:
        if "dbos.enqueue_workflow(" in statement:
            active[:] = [conn]
            if fault:
                fault[0]()
                raise InterruptedOrder()

    def reading(sql: str) -> list[tuple[Any, ...]]:
        if active and active[0].in_transaction():
            return [tuple(row) for row in active[0].execute(sa.text(sql), {"customer": customer})]
        with observer.connect() as conn:
            return [tuple(row) for row in conn.execute(sa.text(sql), {"customer": customer})]

    def obligations() -> list[str]:
        return [
            row[0]
            for row in reading("SELECT workflow_uuid FROM dbos.workflow_status WHERE queue_name='notification_queue'")
        ]

    def admit() -> list[str]:
        before = set(obligations())
        demo.insert_order(customer, "widget", 1)
        return [identity for identity in obligations() if identity not in before]

    @contextmanager
    def during(checkpoint: Callable[[], None]) -> Iterator[None]:
        fault.append(checkpoint)
        try:
            yield
        finally:
            fault.clear()

    with the_demo_without_its_listener(calls.append):
        sa.event.listen(sa.engine.Engine, "after_cursor_execute", after_statement)
        try:
            with hosted(
                Host(
                    production_packages=frozenset({"transactional-outbox", "transactional_enqueue", "dbos"}),
                    in_transaction=lambda: bool(active and active[0].in_transaction()),
                )
            ):
                yield AdmissionAtomicity(
                    name="DBOS order and notification",
                    admit=admit,
                    observe=lambda: [row[0] for row in reading("SELECT customer FROM orders WHERE customer=:customer")],
                    expected=[customer],
                    obligations=obligations,
                    outstanding=lambda: [
                        row[0]
                        for row in reading(
                            "SELECT workflow_uuid FROM dbos.workflow_status WHERE queue_name='notification_queue' AND status='ENQUEUED'"
                        )
                    ],
                    effects=lambda: len(calls),
                    publications=lambda: nullcontext(()),
                    during=during,
                    expected_error=InterruptedOrder,
                )
        finally:
            sa.event.remove(sa.engine.Engine, "after_cursor_execute", after_statement)
            observer.dispose()


@contextmanager
def notification_replay() -> Iterator[ReplaySafeEffect]:
    # ARRANGE: a real admitted order with its queue listener held back.
    # REAL PRODUCTION: invoke the demo's decorated notification step, outside workflow checkpoint deduplication.
    # EXTERNAL SEAM: the demo's simulated notification port records each delivered message.
    # OBSERVE: notification count for this order; repeating a send is customer-visible.
    sends: list[float] = []
    with (
        the_demo_without_its_listener(sends.append),
        hosted(Host(production_packages=frozenset({"transactional-outbox", "transactional_enqueue", "dbos"}))),
    ):
        yield ReplaySafeEffect(
            name="DBOS order notification step",
            prepare=lambda: demo.insert_order("replay customer", "widget", 1),
            execute=lambda identity: demo.send_order_notification(identity, "replay customer", "widget"),
            observe=lambda _identity: len(sends),
            execution_count_for=lambda _identity: len(sends),
        )


PLACE_ORDER_CONTRACT = ObligationContract(
    name="DBOS transactional-outbox: place order",
    adoption=Adoption.LEGACY,
    profiles={
        Profile.A: Decline(
            "DBOS recovers pending workflows when an executor launches, not on a periodic tick, so there is no "
            "sweep to bind: the process histories below prove that recovery against real deaths instead"
        ),
        Profile.B: NotApplicable(
            "the demo runs one executor, which recovers the workflows it started; no lease is handed between "
            "executors (DBOS Conductor reassigns them in a fleet, outside the demo)"
        ),
        Profile.C: KnownGap(
            "a death after the notification is sent, before DBOS records the step, leaves the send's outcome "
            "unknown to DBOS, which reruns it: the customer is notified twice. DBOS resumes from the last "
            "completed step, as documented; the workflow runs once, but its external effect is at-least-once",
            detect=a_death_after_the_notification,
        ),
        Profile.D: Claim(),
        Profile.E: NotApplicable("each workflow writes one order's status; no two results race"),
        Profile.F: Decline(
            "the obligation is the workflow DBOS enqueues in the order's own transaction, not a fact derived "
            "from orders"
        ),
        Profile.H: Claim(
            gaps={
                "assert_replay_converges": "The actual notification step sends a second customer notification on replay."
            }
        ),
        Profile.J: Claim(),
        Profile.G: NotApplicable(
            "A committed order is immediately eligible for notification; there is no later prerequisite."
        ),
        Profile.I: Claim(),
    },
    admission={"place order": order_admission},
    replay=notification_replay,
    retry=the_notifications_retry,
    retention=the_demos_retention,
    # Placing an order survives a death before the notification: a process handoff of its own.
    process_handoffs=(DEATHS_BEFORE_THE_NOTIFICATION,),
    extras=(
        ExtraProof(
            name="an order whose process died before its workflow ran is still notified",
            run=A_LOST_WORKFLOW_IS_ABSORBED_BY_A_RESTART,
        ),
    ),
)


# The enqueue is SQL inside the order's transaction, which a static scan cannot see,
# so there is no site to cover: the contract stands on its crash histories alone.
@due_work_contract_suite(PLACE_ORDER_CONTRACT)
class TestPlaceOrder:
    pass
