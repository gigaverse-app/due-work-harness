"""
``management_command`` resolves the project's own command and can run it later on the host's clock.

The command is replaced by a recording one, so the test sees what production
code would: the command class runs, its arguments arrive, and time has moved.
"""

from datetime import datetime, timedelta
from unittest import mock

import pytest
from django.core.management.base import BaseCommand, CommandParser
from django.utils.timezone import now

from due_work_harness.integrations.django.commands import management_command


class RecordingCommand(BaseCommand):
    runs: list[tuple[tuple[str, ...], dict[str, str], datetime]] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("label")
        parser.add_argument("--flag", default="off")

    def handle(self, *args: str, **options: str) -> None:
        self.runs.append((args, {"label": options["label"], "flag": options["flag"]}, now()))


@pytest.fixture(autouse=True)
def _project_commands():
    RecordingCommand.runs = []
    with mock.patch("django.core.management.get_commands", return_value={"recording": RecordingCommand()}):
        yield


def test_runs_the_projects_command_with_its_arguments() -> None:
    management_command("recording", "first", flag="on")()

    ((_, options, _),) = RecordingCommand.runs
    assert options == {"label": "first", "flag": "on"}


def test_after_runs_the_command_later_on_the_hosts_frozen_clock() -> None:
    before = now()

    management_command("recording", "later", after=timedelta(days=30))()

    ((_, _, ran_at),) = RecordingCommand.runs
    assert ran_at - before >= timedelta(days=30)
    assert now() - before < timedelta(days=1), "the clock is restored afterwards"


def test_an_unknown_command_names_the_ones_that_exist() -> None:
    with pytest.raises(AssertionError, match=r"no management command 'missing'.*\['recording'\]"):
        management_command("missing")()
