"""Saleor's own test setup, unchanged, then the harness's Django host."""

import runpy
from pathlib import Path

from due_work_harness import configure
from due_work_harness.integrations.django import django_host

SALEOR = Path(__file__).resolve().parents[1] / ".upstream" / "saleor"

# Saleor's root conftest: its database setup and the fixture modules its tests use
# (``pytest_plugins``). Run as it is, so these fixtures are exactly Saleor's.
globals().update(
    {name: value for name, value in runpy.run_path(str(SALEOR / "conftest.py")).items() if not name.startswith("__")}
)

# The system under test is Saleor, reached through Django: bindings must reach one of them.
configure(django_host(production_packages={"saleor", "django"}, lifecycle_proofs=False))
