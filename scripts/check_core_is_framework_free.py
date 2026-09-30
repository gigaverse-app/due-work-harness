"""Import every core module with no framework installed; fail if any framework module got loaded."""

import importlib
import pkgutil
import sys

import due_work_harness

FRAMEWORKS = (
    "django",
    "celery",
    "procrastinate",
    "dbos",
    "sqlalchemy",
    "psycopg",
    "time_machine",
    "asgiref",
    "redis",
    "rq",
    "pymongo",
    "motor",
    "prefect",
    "confluent_kafka",
)
core = [
    m.name
    for m in pkgutil.walk_packages(due_work_harness.__path__, "due_work_harness.")
    if ".integrations" not in m.name
]
for name in core:
    importlib.import_module(name)
loaded = sorted(n for n in sys.modules if n.split(".")[0] in FRAMEWORKS)
assert not loaded, f"core modules loaded frameworks: {loaded}"
print(f"{len(core)} core modules imported with no framework loaded")
