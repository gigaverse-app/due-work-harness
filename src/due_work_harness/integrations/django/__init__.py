"""
Django integration: a host built from Django's database layer.

Install with ``pip install due-work-harness[django]`` and configure once per
test session::

    # conftest.py
    from due_work_harness import configure
    from due_work_harness.integrations.django import django_host

    configure(django_host(production_packages={"myapp"}))

What the host supplies:

* ``database_marks`` — pytest-django's ``django_db`` mark; transactional proofs
  get ``django_db(transaction=True)`` so they observe real commits;
* ``in_transaction`` — ``connection.in_atomic_block`` on the default connection;
* ``worker_killer`` — :func:`.commits.django_worker_killer`, counting commits on
  the calling thread's default connection;
* ``connection_scope`` — ``close_old_connections`` around a racing thread;
* ``selection_inspectors`` — :class:`.selection.DjangoSelectionInspector` for
  QuerySet selections (PostgreSQL plans, replica reads, statement capture);
* ``frozen_clock`` — :func:`~due_work_harness.integrations.clocks.time_machine_clock`;
* ``sweep_proofs`` — the lifecycle-state proofs (2b/2c), which read a QuerySet's
  WHERE clause and the model's ``choices``;
* ``publication_recorder`` — pass one for your queue (for example
  :func:`due_work_harness.integrations.celery.celery_publications`); without one,
  every ``Lifecycle`` must say why its worker publishes nothing.

Beside the host, :mod:`~due_work_harness.integrations.django.lock_order` ships a recorder
and a ``lock_order`` fixture that fail when two transactions lock the same tables in
opposite orders, which deadlock only under concurrency.
"""

import functools
from collections.abc import Callable, Collection, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from due_work_harness.host import Host, PublicationBreaker, ReceiverBreaker
from due_work_harness.integrations.clocks import time_machine_clock


def _database_marks(transactional: bool, *, serialized_rollback: bool = False) -> list[Any]:
    import pytest

    if transactional and serialized_rollback:
        return [pytest.mark.django_db(transaction=True, serialized_rollback=True)]
    return [pytest.mark.django_db(transaction=transactional)]


def _in_transaction() -> bool:
    from django.db import connection

    return connection.in_atomic_block


@contextmanager
def _connection_scope() -> Iterator[None]:
    from django.db import close_old_connections

    close_old_connections()
    try:
        yield
    finally:
        close_old_connections()


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
    from due_work_harness.integrations.django.callbacks import django_callback_breaker
    from due_work_harness.integrations.django.commits import django_worker_killer
    from due_work_harness.integrations.django.selection import DjangoSelectionInspector

    sweep_proofs: tuple[Callable[[Any], None], ...] = ()
    if lifecycle_proofs:
        from due_work_harness.integrations.django.lifecycle_states import TERMINAL_OBLIGATION_PROOFS

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
