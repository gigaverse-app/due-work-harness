"""The reference jobs the RQ self-tests run: one that sends a message and announces it, one whose service is down."""

from collections import Counter
from typing import Any


class Outbox:
    """EXTERNAL SEAM: where the jobs' messages go, and what their callbacks announced."""

    def __init__(self) -> None:
        self.sent: Counter[str] = Counter()
        self.announced: list[str] = []
        self.failed_calls = 0

    def send(self, message: str) -> None:
        self.sent[message] += 1

    def clear(self) -> None:
        self.sent.clear()
        self.announced.clear()
        self.failed_calls = 0


outbox = Outbox()


def send_message(message: str) -> str:
    outbox.send(message)
    return message


def announce_success(job: Any, connection: Any, result: Any) -> None:
    outbox.announced.append(f"sent {result!r}")


def announce_failure(job: Any, connection: Any, exc_type: type[BaseException], *_exc: Any) -> None:
    outbox.announced.append(f"failed: {exc_type.__name__}")


def call_down_service() -> None:
    outbox.failed_calls += 1
    raise ConnectionError("the service is down")
