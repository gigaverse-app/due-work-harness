import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from due_work_harness.host import Host, hosted
from due_work_harness.references import in_memory_handoffs

# The stand-in production package lives outside ``tests/``: the binding
# tripwires treat any path with a ``tests`` directory as test code.
_SUPPORT = Path(__file__).resolve().parents[2] / "tests_support"
if str(_SUPPORT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT))


@pytest.fixture
def ledger_host() -> Iterator[Host]:
    """A host whose worker killer interrupts the reference ledger's commits."""
    in_memory_handoffs.reset()
    with hosted(Host(worker_killer=in_memory_handoffs.ledger_killer)) as host:
        yield host
    in_memory_handoffs.reset()
