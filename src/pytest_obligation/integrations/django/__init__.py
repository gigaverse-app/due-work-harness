"""
Django integration: a host built from Django's database layer.

Install with ``pip install pytest-obligation[django]`` and configure once per
test session::

    # conftest.py
    from pytest_obligation import configure
    from pytest_obligation.integrations.django import django_host

    configure(django_host(production_packages={"myapp"}))

What the host supplies:

* ``database_marks`` — pytest-django's ``django_db`` mark; transactional proofs
  get ``django_db(transaction=True)`` so they observe real commits;
* ``in_transaction`` — ``connection.in_atomic_block`` on the default connection;
* ``worker_killer`` — :func:`.commits.django_worker_killer`, counting commits on
  the calling thread's default connection;
* ``connection_scope`` — a racing thread's own connection, its lock waits bounded at
  half the race's deadline on PostgreSQL, closed on every exit;
* ``selection_inspectors`` — :class:`.selection.DjangoSelectionInspector` for
  QuerySet selections (PostgreSQL plans, replica reads, statement capture);
* ``frozen_clock`` — :func:`~pytest_obligation.integrations.clocks.time_machine_clock`;
* ``sweep_proofs`` — the lifecycle-state proofs (2b/2c), which read a QuerySet's
  WHERE clause and the model's ``choices``;
* ``publication_recorder`` — pass one for your queue (for example
  :func:`pytest_obligation.integrations.celery.celery_publications`); without one,
  every ``Lifecycle`` must say why its worker publishes nothing.

Beside the host, :mod:`~pytest_obligation.integrations.django.lock_order` ships a recorder
and a ``lock_order`` fixture that fail when two transactions lock the same tables in
opposite orders, which deadlock only under concurrency.
"""

import functools
from collections.abc import Callable, Collection, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from pytest_obligation.host import Host, PublicationBreaker, ReceiverBreaker
from pytest_obligation.integrations.clocks import time_machine_clock


def _database_marks(transactional: bool, *, serialized_rollback: bool = False) -> list[Any]:
    import pytest

    if transactional and serialized_rollback:
        return [pytest.mark.django_db(transaction=True, serialized_rollback=True)]
    return [pytest.mark.django_db(transaction=transactional)]


def _in_transaction() -> bool:
    from django.db import connection

    return connection.in_atomic_block


#: The race deadline a racing thread's database waits are bounded from when the proof
#: that started it gives none, in seconds (FencedOwnership.race_timeout's default).
DEFAULT_RACE_TIMEOUT = 10.0


@contextmanager
def _connection_scope() -> Iterator[None]:
    """
    A racing thread's own connection: bounded while it runs, closed on every exit.

    On PostgreSQL the bounds come from the race's deadline
    (:func:`~pytest_obligation.host.race_timeout`): ``lock_timeout`` is half of it,
    so a claim blocked on the other connection's lock fails with the database's
    error well before the race gives up on it, and ``statement_timeout`` is all of
    it, so nothing the racer started outlives the race on the server.

    ``close_old_connections`` closes only connections past their ``CONN_MAX_AGE``, so
    a persistent one would outlive the thread that owned it; ``close_all`` always
    closes this thread's connections.
    """
    from django.db import connection, connections

    from pytest_obligation.host import race_timeout

    race = race_timeout() or DEFAULT_RACE_TIMEOUT
    try:
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, false), set_config('statement_timeout', %s, false)",
                    [f"{int(race * 500)}ms", f"{int(race * 1000)}ms"],
                )
        yield
    finally:
        connections.close_all()


def django_host(
    production_packages: Collection[str],
    *,
    replica_aliases: Collection[str] | Callable[[], Collection[str]] | None = None,
    ambient_context: Callable[[], object] | None = None,
    publication_recorder: Callable[[], AbstractContextManager[list[str]]] | None = None,
    publication_breaker: PublicationBreaker | None = None,
    receiver_breaker: ReceiverBreaker | None = None,
    lifecycle_proofs: bool = True,
    serialized_rollback: bool = False,
) -> Host:
    """
    A host for a Django project whose own code lives in ``production_packages``.

    ``serialized_rollback`` is for a project whose migrations seed rows its code
    needs, as a CMS's root page, default site or root collection: every case that
    commits for real flushes the database afterwards, and the flush would delete
    them. With it, pytest-django restores the database's migrated contents after
    each such case (it needs the test database created in the session, not reused).
    """
    from pytest_obligation.integrations.django.callbacks import django_callback_breaker
    from pytest_obligation.integrations.django.commits import django_worker_killer
    from pytest_obligation.integrations.django.selection import DjangoSelectionInspector

    sweep_proofs: tuple[Callable[[Any], None], ...] = ()
    if lifecycle_proofs:
        from pytest_obligation.integrations.django.lifecycle_states import TERMINAL_OBLIGATION_PROOFS

        sweep_proofs = TERMINAL_OBLIGATION_PROOFS
    return Host(
        production_packages=frozenset(production_packages),
        database_marks=functools.partial(_database_marks, serialized_rollback=serialized_rollback),
        in_transaction=_in_transaction,
        worker_killer=django_worker_killer,
        callback_breaker=django_callback_breaker,
        connection_scope=_connection_scope,
        selection_inspectors=(DjangoSelectionInspector(replica_aliases=replica_aliases),),
        ambient_context=ambient_context,
        publication_recorder=publication_recorder,
        publication_breaker=publication_breaker,
        receiver_breaker=receiver_breaker,
        frozen_clock=time_machine_clock,
        sweep_proofs=sweep_proofs,
    )


__all__ = ["django_host"]
