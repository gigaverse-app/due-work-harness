"""
``management_command`` resolves the project's own command, runs it later on the host's clock, and refuses test code.

The production command is Django's ``remove_stale_contenttypes`` with its ``handle`` recorded, so the
test sees what production would: the command class runs, its options arrive, and time has moved.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.contrib.contenttypes.management.commands.remove_stale_contenttypes import Command
from django.core.management.base import BaseCommand
from django.utils.timezone import now

from pytest_obligation import configure
from pytest_obligation.integrations.django import django_host
from pytest_obligation.integrations.django.commands import management_command

COMMAND = "remove_stale_contenttypes"

#: Each ``handle`` call of the recorded production command: its options, and the time the application saw.
Runs = list[tuple[dict[str, Any], datetime]]


class CommandDefinedInATest(BaseCommand):
    """Defined in a test module, so recovery built from it would certify nothing."""

    def handle(self, *args: Any, **options: Any) -> None:
        raise AssertionError("a test-defined command must be refused before it runs")


@pytest.fixture
def runs() -> Iterator[Runs]:
    configure(django_host(production_packages={"contenttypes"}))
    recorded: Runs = []

    def handle(command: Command, *args: Any, **options: Any) -> None:
        recorded.append((options, now()))

    with mock.patch.object(Command, "handle", handle):
        yield recorded


def test_runs_the_projects_command_with_its_options(runs: Runs) -> None:
    management_command(COMMAND, verbosity=0)()

    ((options, _),) = runs
    assert options["verbosity"] == 0


def test_after_runs_the_command_later_on_the_hosts_frozen_clock(runs: Runs) -> None:
    before = now()

    management_command(COMMAND, after=timedelta(days=30))()

    ((_, ran_at),) = runs
    assert ran_at - before >= timedelta(days=30)
    assert now() - before < timedelta(days=1), "the clock is restored afterwards"


def test_an_unknown_command_names_the_ones_that_exist(runs: Runs) -> None:
    with pytest.raises(AssertionError, match=rf"no management command 'missing'.*'{COMMAND}'"):
        management_command("missing")()


def test_a_command_defined_in_test_code_is_refused_as_recovery(runs: Runs) -> None:
    with mock.patch(
        "pytest_obligation.integrations.django.commands.get_commands", return_value={"mine": CommandDefinedInATest()}
    ):
        with pytest.raises(
            AssertionError, match=r"management command 'mine' is .*CommandDefinedInATest.*production packages"
        ):
            management_command("mine")()
