"""On another database the commit counter says so, instead of failing on PostgreSQL-only SQL."""

from unittest import mock

import pytest
from django.db import DEFAULT_DB_ALIAS, connections

from due_work_harness.integrations.django.commits import django_worker_killer


def test_the_commit_counter_refuses_a_database_that_is_not_postgresql() -> None:
    with mock.patch.object(connections[DEFAULT_DB_ALIAS], "vendor", "sqlite"):
        with pytest.raises(AssertionError, match=r"needs PostgreSQL .*'default' is 'sqlite'"):
            with django_worker_killer(None):
                pass
