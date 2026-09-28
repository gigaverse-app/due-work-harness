"""
Migrate Saleor's test database once, then give each xdist worker a copy of it.

pytest-django creates a test database per xdist worker, and each one runs all
of Saleor's migrations. On a four-CPU runner, four migrations at once took 193
seconds each, twice as long as one alone. Migrating once and cloning the result
through PostgreSQL's template databases costs one migration; the test run then
passes ``--reuse-db``, and each worker finds its database ready.

Run with Saleor's interpreter, as ``run.sh --prepare-db N`` does, with
``DATABASE_URL`` set as for the tests.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

SALEOR = Path(__file__).resolve().parents[1] / ".upstream" / "saleor"


def _with_database(url: str, name: str) -> str:
    return urlsplit(url)._replace(path=f"/{name}").geturl()


def main(workers: int) -> None:
    url = os.environ["DATABASE_URL"]
    name = urlsplit(url).path.lstrip("/")
    # pytest-django's names: test_<NAME>, with _gw<i> per xdist worker.
    migrated = f"test_{name}_migrated"
    clones = [f"test_{name}_gw{worker}" for worker in range(workers)]

    with psycopg.connect(_with_database(url, "postgres"), autocommit=True) as server:
        server.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(migrated)))
        server.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(migrated)))

    # Saleor's own migrations, under its test settings, exactly as pytest-django would run them.
    subprocess.run(
        [sys.executable, "manage.py", "migrate", "--no-input", "--verbosity", "0"],
        cwd=SALEOR,
        env={
            **os.environ,
            "DATABASE_URL": _with_database(url, migrated),
            "DJANGO_SETTINGS_MODULE": "saleor.tests.settings",
        },
        check=True,
    )

    with psycopg.connect(_with_database(url, "postgres"), autocommit=True) as server:
        for clone in clones:
            server.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(clone)))
            server.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(clone), sql.Identifier(migrated))
            )
    print(f"migrated {migrated} once; cloned it into {', '.join(clones)}")


if __name__ == "__main__":
    main(int(sys.argv[1]))
