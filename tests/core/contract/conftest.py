from collections.abc import Iterator

import pytest

from pytest_obligation.host import Host, hosted


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
