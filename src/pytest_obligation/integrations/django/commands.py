"""
Run a Django management command as recovery, or as the transition, by name.

Most Django projects recover work with a management command that cron or a
scheduler runs (``runperiodic``, ``process_tasks``, ``send_queued_mail``). A
binding that calls ``call_command("runperiodic")`` reaches no project code the
harness can see, and is refused as test-authored; importing the command class
to satisfy it, and time-travelling to when cron would run it, is the same few
lines in every adopter. :func:`management_command` is that once: it resolves the
name to the project's own command class, and can run it a while later on the
host's frozen clock (a payment term that has to pass, a retry that has to
come due).

The harness treats its own callables as trusted, so the helper checks what a
binding built from it could not: that the command it resolved is production
code. A command defined in a test app would otherwise pass as recovery.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.core.management import BaseCommand, call_command, get_commands, load_command_class
from django.utils.timezone import now

from pytest_obligation.binding import is_production_value
from pytest_obligation.host import current_host, production_packages


def management_command(
    name: str, *arguments: str, after: timedelta | None = None, **options: Any
) -> Callable[..., None]:
    """
    Recovery (or a transition) that runs the management command ``name``.

    ``after`` runs it that long from now, on the host's ``frozen_clock`` (the
    Django host supplies one when ``time-machine`` is installed): cron running
    it at a later time. The command is the project's own, looked up the way
    ``manage.py`` finds it, and must live in one of the host's
    ``production_packages``; an unknown name fails with the commands that
    exist. ``after`` is this helper's own keyword, so a command option of that
    name cannot be passed through. The result ignores what it is given, so it
    can be a handoff's ``recover``.
    """

    def run(*_given: object) -> None:
        command = _production_command(name)
        if after is None:
            call_command(command, *arguments, **options)
            return
        # Cron running the command later: the application reads the later time, the test does not wait.
        with current_host().require("frozen_clock")(now() + after):
            call_command(command, *arguments, **options)

    return run


def _production_command(name: str) -> BaseCommand:
    """The command ``manage.py <name>`` would run, provided it is the project's own code."""
    commands = get_commands()
    assert name in commands, f"no management command {name!r}; the project has {sorted(commands)}"
    # ``get_commands`` maps a name to its app's module path, or to a ready command instance.
    source = commands[name]
    command = load_command_class(source, name) if isinstance(source, str) else source
    # The check the binding guard cannot make for us: it trusts this helper, so a command defined in a
    # test app would pass as recovery unless the helper refuses it here.
    assert is_production_value(type(command)), (
        f"management command {name!r} is {type(command).__module__}.{type(command).__qualname__}, which is not "
        f"in the host's production packages {sorted(production_packages())}: recovery must be production code"
    )
    return command


__all__ = ["management_command"]
