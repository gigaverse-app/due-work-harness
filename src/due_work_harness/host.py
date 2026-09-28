"""
The host: everything a framework supplies to the framework-free proofs.

The proofs in this package state invariants about due work — work a program
records now and some worker completes later — over plain callables: a selection
returns identities, a tick runs, a transition executes, an observation compares.
Nothing in them imports a web framework, an ORM, a queue or a workflow engine.

A few proofs need facts only the application's framework can supply: whether
the calling thread is inside a database transaction, how to count and interrupt
its commits, what query plan a selection runs, which pytest marks give a test a
database. :class:`Host` gathers those capabilities. Each is optional; a proof
that needs a missing capability fails with a message naming it, rather than
guessing. Integrations build hosts for their frameworks, for example
:func:`due_work_harness.integrations.django.django_host`.

Configure a host once per test session, either in ``conftest.py``::

    from due_work_harness import configure
    from due_work_harness.integrations.django import django_host

    configure(django_host(production_packages={"myapp"}))

or in the pytest configuration file::

    [pytest]
    due_work_harness_host = myapp.testing:harness_host
"""

from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import datetime
from typing import Any, Protocol

from pydantic import Field, SkipValidation

from due_work_harness.models import HarnessModel


class WorkerKiller(Protocol):
    """
    Counts the commits a worker makes and kills it inside the chosen one.

    ``kill_after`` is the 1-based commit to die in, or ``None`` to only count.
    The context yields an object whose ``commits`` attribute is the count so far
    and whose ``dead`` attribute says whether the death happened. Once dead,
    every later statement on the worker's connection must raise
    :class:`~due_work_harness.worker_death.WorkerDied`, and callbacks registered
    to run after the commit must be dropped, as a dead process's would be.
    ``kill_now(reason)`` kills the worker at an arbitrary point, such as right
    after an external call returns.
    """

    def __call__(self, kill_after: int | None) -> AbstractContextManager[Any]: ...


class CountedFaults(Protocol):
    """
    What a fault breaker's context yields while a transition runs.

    ``count`` is how many of the family's occurrences have run so far: callbacks
    run, messages published, receivers called. ``failure`` is the exception the
    chosen occurrence raised instead of running, or ``None`` while none has. The
    crash histories drive every family the same way from these two.
    """

    count: int
    failure: BaseException | None


class CallbackBreaker(Protocol):
    """
    Counts the after-commit callbacks a worker runs, and makes the chosen one fail.

    ``fail_at`` is the 1-based callback to fail, or ``None`` to only count. A
    callback counts when it runs, not when it is registered, so one discarded
    with a rolled-back savepoint never counts. The context yields
    :class:`CountedFaults`. The chosen callback raises
    :class:`~due_work_harness.worker_death.CallbackFailed` instead of running;
    everything after that is the framework's own behaviour.
    """

    def __call__(self, fail_at: int | None) -> AbstractContextManager[Any]: ...


class ReceiverBreaker(Protocol):
    """
    Counts the signal (or hook) receivers a worker runs, and makes the chosen one fail.

    ``fail_at`` is the 1-based receiver to fail, or ``None`` to only count. The
    context yields :class:`CountedFaults`. The chosen receiver raises
    :class:`~due_work_harness.worker_death.ReceiverFailed` instead of running;
    everything after that is the framework's own dispatch.
    """

    def __call__(self, fail_at: int | None) -> AbstractContextManager[Any]: ...


class PublicationBreaker(Protocol):
    """
    Counts the messages a worker publishes to its broker, and makes the chosen publish fail.

    ``refuse_at`` is the 1-based publication to refuse, or ``None`` to only
    count. The context yields :class:`CountedFaults`. The refused publish raises
    the broker client's
    own connection error, as a broker that is down or drops the connection
    would, while the process lives on; everything after that is the
    application's own behaviour.
    """

    def __call__(self, refuse_at: int | None) -> AbstractContextManager[Any]: ...


class SelectionInspector(Protocol):
    """
    Facts about a selection that only the database can answer.

    Every selection method receives the object the adopter's ``due_work``
    binding returned. ``understands`` says whether this inspector can read it
    at all; a proof that needs an inspector and finds none that understands the
    selection fails, naming the missing capability. A method that cannot answer
    truthfully for this database (no plan support, a replica it cannot tell
    from the primary) raises ``AssertionError`` saying why, rather than guess.
    """

    def understands(self, selection: object) -> bool: ...

    def index_served(self, selection: object) -> tuple[bool, str]:
        """
        Whether an index narrows every scan of the selection's own table.

        The text is the evidence: when not served, why not, including the plan.
        """
        ...

    def replica_read(self, selection: object) -> str | None:
        """The replica the selection reads from, or ``None`` when it reads the primary."""
        ...

    def scan_counts(self, selection: object) -> tuple[float, float]:
        """Executing the selection: ``(rows returned, rows read and discarded by filters)``."""
        ...

    def statements_during(self, run: Callable[[], object]) -> list[str]:
        """The database statements ``run`` executed, as SQL text, in order."""
        ...


class Host(HarnessModel):
    """What the application's framework supplies. Every field is optional."""

    #: Top-level package names whose code is the system under test. Adapter
    #: bindings must reach code in one of them; see :mod:`due_work_harness.binding`.
    production_packages: frozenset[str] = frozenset()

    #: pytest marks a generated case needs to use the database. ``True`` asks for
    #: real commits (no test-wrapping transaction), which proofs that observe
    #: transaction state, race connections or crash after commits require.
    database_marks: Callable[[bool], Sequence[Any]] = lambda _transactional: ()

    #: Whether the calling thread's database connection is inside a transaction.
    in_transaction: Callable[[], bool] | None = None

    #: Counts and interrupts commits on the calling thread's connection; crash
    #: histories need it to kill a worker right after each commit. A protocol,
    #: not a runtime-checkable class, so Pydantic does not check it.
    worker_killer: SkipValidation[WorkerKiller | None] = None

    #: Counts the after-commit callbacks a worker runs and fails the chosen one;
    #: crash histories use it to fail each callback in turn. Optional: without
    #: it, that family of histories does not run.
    callback_breaker: SkipValidation[CallbackBreaker | None] = None

    #: Counts the messages a worker publishes and refuses the chosen one, as a
    #: broker that is down would; crash histories refuse each publication in
    #: turn. Optional: without it, that family of histories does not run.
    publication_breaker: SkipValidation[PublicationBreaker | None] = None

    #: Counts the receivers of the signals an adopter names and fails the chosen
    #: one; crash histories fail each receiver in turn. Optional: without it,
    #: that family of histories does not run.
    receiver_breaker: SkipValidation[ReceiverBreaker | None] = None

    #: The connection lifecycle of a thread a proof starts, for example to race
    #: two claims on two real connections.
    connection_scope: Callable[[], AbstractContextManager[None]] = nullcontext

    #: Database facts about a selection: index use, replica reads, scan ratio.
    #: Protocol implementations, so Pydantic does not check them.
    selection_inspectors: SkipValidation[tuple[SelectionInspector, ...]] = ()

    #: Ambient per-call context a recovery tick must restore, such as the
    #: current tenant. ``None`` skips the proof that checks it.
    ambient_context: Callable[[], object] | None = None

    #: Records work published to a queue outside the database without sending
    #: it: a context manager yielding the list of published task names. Proofs
    #: that must see a handoff made only as a message need it.
    publication_recorder: Callable[[], AbstractContextManager[list[str]]] | None = None

    #: Freezes the application's clock at a moment (a context manager). Proofs
    #: that age rows past a recovery delay need it.
    frozen_clock: Callable[[datetime], AbstractContextManager[Any]] | None = None

    #: Profile A proofs only a framework can run, appended to every sweep's
    #: generated suite — the Django host adds the lifecycle-state proofs.
    sweep_proofs: tuple[Callable[[Any], None], ...] = ()

    #: Anything else an integration wants to carry; the core never reads it.
    extras: dict[str, Any] = Field(default_factory=dict)

    def inspector_for(self, selection: object, *, capability: str) -> SelectionInspector:
        for inspector in self.selection_inspectors:
            if inspector.understands(selection):
                return inspector
        raise AssertionError(
            f"{capability} needs a SelectionInspector that understands {type(selection).__name__}, and the "
            f"configured host has none. Use an integration's host (for example "
            f"due_work_harness.integrations.django.django_host) or add an inspector for this selection type"
        )

    def require(self, capability: str) -> Any:
        value = getattr(self, capability)
        assert value is not None, (
            f"this proof needs the host capability {capability!r}, and the configured host does not supply it. "
            f"Configure a host from an integration (for example due_work_harness.integrations.django.django_host) "
            f"or supply {capability} yourself"
        )
        return value


_host = Host()


def configure(host: Host) -> None:
    """Install the host every proof reads. Call once per test session."""
    global _host
    _host = host


def current_host() -> Host:
    return _host


@contextmanager
def hosted(host: Host) -> Iterator[Host]:
    """Use ``host`` for the duration of a block; for tests of the harness itself."""
    previous = current_host()
    configure(host)
    try:
        yield host
    finally:
        configure(previous)


def packages(*names: str) -> frozenset[str]:
    return frozenset(names)


def production_packages() -> Collection[str]:
    return current_host().production_packages
