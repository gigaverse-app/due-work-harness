"""
Which Redis databases the Redis-backed self-tests use: one set per pytest-xdist worker.

Every RQ case empties its database, and every Celery case its broker, result
backend and records, so two workers sharing one would wipe each other's work.
Run serially, the suites keep their fixed databases (RQ 14, Celery 10 to 12, or
the URLs in ``REDIS_URL`` and ``CELERY_*``); under xdist, worker ``gwN`` takes
databases ``4N`` to ``4N + 3`` on ``REDIS_URL``'s server, so four workers use all
16 of a default Redis. A Celery worker's child processes inherit
``PYTEST_XDIST_WORKER``, so they read the same databases as the test that ran them.
"""

import os
from urllib.parse import urlsplit, urlunsplit

_ROLES = ("rq", "celery broker", "celery backend", "celery records")
_SERIAL = {
    "rq": ("REDIS_URL", 14),
    "celery broker": ("CELERY_BROKER_URL", 10),
    "celery backend": ("CELERY_RESULT_BACKEND", 11),
    "celery records": ("CELERY_RECORDS_URL", 12),
}
#: A default Redis has databases 0 to 15.
_DATABASES = 16


def redis_url(role: str) -> str:
    """The URL of the database ``role`` uses in this test process."""
    variable, database = _SERIAL[role]
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        return os.environ.get(variable) or _with_database(_server(), database)
    database = int(worker.removeprefix("gw")) * len(_ROLES) + _ROLES.index(role)
    assert database < _DATABASES, (
        f"xdist worker {worker} would need Redis database {database}; each worker takes {len(_ROLES)} of a "
        f"default Redis's {_DATABASES}, so run the Redis suites with at most {_DATABASES // len(_ROLES)} workers"
    )
    return _with_database(_server(), database)


def _server() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379")


def _with_database(url: str, database: int) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))
