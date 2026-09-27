import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "database_for_exemption: stand-in for a host's database mark in self-tests")
