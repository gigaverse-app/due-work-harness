"""Small real SQL command used to discriminate admission guarantees and their counterfeits."""

from collections.abc import Callable
from sqlite3 import Connection
from typing import Literal

from due_work_harness.models import MutableHarnessModel

AdmissionFault = Literal[
    "none",
    "no_transaction",
    "partial_commit",
    "left_open",
    "publish_early",
    "publish_on_failure",
    "external_effect",
    "missing_work",
    "wrong_product",
    "no_checkpoint",
    "unrelated_error",
]


class InterruptedAdmission(Exception):
    """The injected failure after both halves of admission have been written."""


class Command(MutableHarnessModel):
    db: Connection
    fault: AdmissionFault = "none"
    checkpoint: Callable[[], None] | None = None
    publish: Callable[[], None] = lambda: None
    provider_calls: int = 0

    def admit(self) -> tuple[int, ...]:
        """Persist intent and obligation as one transaction; wakeup follows commit."""
        if self.fault != "no_transaction":
            self.db.execute("BEGIN")
        try:
            self.db.execute("UPDATE product SET requested = 1")
            if self.fault != "missing_work":
                self.db.execute("INSERT INTO obligations (id) VALUES (1)")
            if self.fault == "external_effect":
                self.provider_calls += 1
            if self.fault == "publish_early":
                self.publish()
            if self.fault == "partial_commit":
                self.db.commit()
            if self.fault == "unrelated_error":
                raise ValueError("unexpected command failure")
            if self.checkpoint is not None and self.fault != "no_checkpoint":
                self.checkpoint()
            if self.fault == "wrong_product":
                self.db.execute("UPDATE product SET requested = 2")
            self.db.commit()
        except InterruptedAdmission:
            self.db.rollback()
            if self.fault == "left_open":
                self.db.execute("BEGIN")
            if self.fault == "publish_on_failure":
                self.publish()
            raise
        self.publish()
        return (1,)
