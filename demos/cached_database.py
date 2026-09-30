"""
A migrated PostgreSQL database, restored from a dump when CI's cache holds one.

Migrating Saleor's or Wagtail's test database is most of a demo job's time, and
what the migration produces depends only on the pinned upstream code. So CI
caches a ``pg_dump`` of the result, keyed on the pins: a hit restores it in
seconds, and a miss (a pin moved, or the first run) migrates and writes the dump
for the next run. The dump is only ever a copy of what the migration produced
under the same key, so the tests see the same database either way.

Usage, from any interpreter with psycopg::

    python demos/cached_database.py NAME [--dump PATH] -- MIGRATE_COMMAND...

The database server is ``DATABASE_URL``'s, or libpq's ``PG*`` variables'. NAME is
dropped and created empty, then the migrate command runs with ``PGDATABASE`` (and
``DATABASE_URL``, when set) pointing at it.
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql


def with_database(url: str, name: str) -> str:
    return urlsplit(url)._replace(path=f"/{name}").geturl()


def connection_string(name: str) -> str:
    """How libpq reaches database ``name``: on ``DATABASE_URL``'s server, else on the ``PG*`` variables' one."""
    url = os.environ.get("DATABASE_URL")
    return with_database(url, name) if url else f"dbname={name}"


def migrated_database(name: str, migrate: Sequence[str], *, dump: Path | None, cwd: Path | None = None) -> str:
    """Create ``name`` migrated: restored from ``dump`` when it exists, else by ``migrate``, then dumped."""
    target = connection_string(name)
    with psycopg.connect(connection_string("postgres"), autocommit=True) as server:
        server.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    if dump is not None and dump.exists():
        subprocess.run(["pg_restore", "--no-owner", "--exit-on-error", "--dbname", target, str(dump)], check=True)
        return f"restored {name} from {dump}"
    pointed = {"PGDATABASE": name} | ({"DATABASE_URL": target} if "DATABASE_URL" in os.environ else {})
    subprocess.run(migrate, cwd=cwd, env={**os.environ, **pointed}, check=True)
    if dump is None:
        return f"migrated {name}"
    dump.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pg_dump", "--format=custom", "--file", str(dump), target], check=True)
    return f"migrated {name}, and saved {dump} for the next run"


def main(argv: Sequence[str]) -> None:
    assert "--" in argv, "give the migrate command after --"
    split = list(argv).index("--")
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    parser.add_argument("name")
    parser.add_argument("--dump", type=Path)
    arguments = parser.parse_args(argv[:split])
    migrate = argv[split + 1 :]
    assert migrate, "give the migrate command after --"
    print(migrated_database(arguments.name, migrate, dump=arguments.dump))


if __name__ == "__main__":
    main(sys.argv[1:])
