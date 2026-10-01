"""Generated proofs against the runnable SQLite catalog application."""

from test_bindings import catalog_contract

from pytest_obligation import due_work_contract_suite

CATALOG = catalog_contract("example catalog")


@due_work_contract_suite(CATALOG)
class TestCatalog:
    pass
