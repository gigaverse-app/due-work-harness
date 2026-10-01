"""The renamed distribution still loads the existing public API and plugin."""

from importlib.metadata import distribution

import pytest

import pytest_obligation
from pytest_obligation.coverage.cli import main

PACKAGE = "pytest-obligation"


def test_distribution_version_matches_public_api() -> None:
    assert pytest_obligation.__version__ == distribution(PACKAGE).version


@pytest.mark.parametrize("command", ["pytest-obligation", "due-work-harness"])
def test_new_and_legacy_console_commands_load_same_runner(command: str) -> None:
    entry = next(e for e in distribution(PACKAGE).entry_points if e.group == "console_scripts" and e.name == command)
    assert entry.load() is main


def test_distribution_exposes_discoverable_pytest_plugin() -> None:
    entry = next(e for e in distribution(PACKAGE).entry_points if e.group == "pytest11")
    assert entry.load().__name__ == "pytest_obligation.pytest_plugin"
