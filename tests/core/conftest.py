from collections.abc import Iterator

import pytest

from due_work_harness.host import Host, hosted
from due_work_harness.references import in_memory_handoffs


@pytest.fixture
def ledger_host() -> Iterator[Host]:
    """A host whose worker killer interrupts the reference ledger's commits."""
    in_memory_handoffs.reset()
    with hosted(Host(worker_killer=in_memory_handoffs.ledger_killer)) as host:
        yield host
    in_memory_handoffs.reset()
