import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from due_work_harness.host import Host, hosted

# The stand-in production package lives outside ``tests/``: the binding
# tripwires treat any path with a ``tests`` directory as test code.
_SUPPORT = Path(__file__).resolve().parents[3] / "tests_support"
if str(_SUPPORT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "database(transaction): the database mark a self-test host hands to generated cases"
    )


@pytest.fixture
def production_host() -> Iterator[Host]:
    """A host whose production code is the stand-in ``sample_production`` package."""
    with hosted(Host(production_packages=frozenset({"sample_production"}))) as host:
        yield host


@pytest.fixture
def marking_host() -> Iterator[Host]:
    """A host that gives generated cases a database mark recording whether they need real commits."""
    with hosted(Host(database_marks=lambda transactional: [pytest.mark.database(transaction=transactional)])) as host:
        yield host
