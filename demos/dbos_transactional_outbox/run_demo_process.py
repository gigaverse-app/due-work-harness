"""
FAULT INJECTION: one run of the unmodified demo in its own process, dying where told.

The child starts the application through the demo's own ``main()`` and places
one order through its ``create_order`` endpoint. ``uvicorn.run`` is the only
replacement: instead of serving HTTP it places the order and waits.
The parent chooses the death through due-work-harness's child protocol
(``process_histories.fault_environment``), and the child dies with ``die_here``:

* no fault        - wait until the notification is marked sent, then exit 0;
* ``after_order`` - right after the order and its workflow committed;
* ``before_send`` - when the notification step starts, before sending;
* ``after_send``  - right after the send returns, before DBOS records the step.

Each send appends a line to ``SENDS_FILE``: that file is the customer's inbox.
"""

import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

from due_work_harness.process_histories import die_here

sys.path.insert(0, os.environ["DEMO_DIR"])
import transactional_enqueue as demo  # noqa: E402


def send(_seconds: float) -> None:
    # EXTERNAL SEAM: the demo's time.sleep(3) simulates the network call that sends the notification.
    die_here("before_send")
    with open(os.environ["SENDS_FILE"], "a") as inbox:
        inbox.write(f"{os.environ['CUSTOMER']}\n")
    die_here("after_send")


def serve(_app: object, **_kwargs: object) -> None:
    order_id = demo.create_order(demo.OrderRequest(customer=os.environ["CUSTOMER"], item="widget", quantity=1))[
        "order_id"
    ]
    print(f"ORDER {order_id}", flush=True)
    die_here("after_order")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if next(o for o in demo.list_orders() if o["order_id"] == order_id)["notification_status"] == "SENT":
            os._exit(0)
        time.sleep(0.2)
    os._exit(3)


with (
    mock.patch.object(demo, "time", SimpleNamespace(sleep=send)),
    mock.patch.object(demo, "uvicorn", SimpleNamespace(run=serve)),
):
    demo.main()
