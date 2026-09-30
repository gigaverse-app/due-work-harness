"""
Migrate Saleor's test database once, then give each xdist worker a copy of it.

pytest-django creates a test database per xdist worker, and each one runs all
of Saleor's migrations. On a four-CPU runner, four migrations at once took 193
seconds each, twice as long as one alone. Migrating once and cloning the result
through PostgreSQL's template databases costs one migration; the test run then
passes ``--reuse-db``, and each worker finds its database ready.

Run with Saleor's interpreter, as ``run.sh --prepare-db N`` does, with
``DATABASE_URL`` set as for the tests. With ``SALEOR_DATABASE_DUMP`` set, the
migrated database is restored from that dump when it exists and written to it
when not (see ``demos/cached_database.py``); CI caches it on Saleor's pin.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cached_database import migrated_database, with_database  # noqa: E402 - the demos directory, added above

SALEOR = Path(__file__).resolve().parents[1] / ".upstream" / "saleor"


def main(workers: int) -> None:
    url = os.environ["DATABASE_URL"]
    name = urlsplit(url).path.lstrip("/")
    # pytest-django's names: test_<NAME>, with _gw<i> per xdist worker.
    migrated = f"test_{name}_migrated"
    clones = [f"test_{name}_gw{worker}" for worker in range(workers)]

    dump = os.environ.get("SALEOR_DATABASE_DUMP")
    # Saleor's own migrations, under its test settings, exactly as pytest-django would run them.
    os.environ["DJANGO_SETTINGS_MODULE"] = "saleor.tests.settings"
    made = migrated_database(
        migrated,
        [sys.executable, "manage.py", "migrate", "--no-input", "--verbosity", "0"],
        dump=Path(dump) if dump else None,
        cwd=SALEOR,
    )

    with psycopg.connect(with_database(url, "postgres"), autocommit=True) as server:
        for clone in clones:
            server.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(clone)))
            server.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(clone), sql.Identifier(migrated))
            )
    print(f"{made}; cloned it into {', '.join(clones)}")


if __name__ == "__main__":
    main(int(sys.argv[1]))
