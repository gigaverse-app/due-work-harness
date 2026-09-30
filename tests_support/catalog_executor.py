"""Local worker seam for running a live application binding through queue machinery.

The RQ SimpleWorker and Celery eager tracer run in this process so accepted
provider requests remain controllable. Separate existing worker-history suites
exercise process death and real broker redelivery; this is not a substitute.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import uuid4

OPERATIONS: dict[str, Callable[[], None]] = {}


def execute(identity: str) -> None:
    OPERATIONS[identity]()


@contextmanager
def registered(operation: Callable[[], None]) -> Iterator[str]:
    identity = uuid4().hex
    OPERATIONS[identity] = operation
    try:
        yield identity
    finally:
        del OPERATIONS[identity]
