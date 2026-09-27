"""
The coverage check: every handoff site in production has exactly one disposition.

Run it from CI with ``due-work-harness check`` (or the ``check`` GitHub Action),
or from a test::

    from due_work_harness.coverage import assert_every_site_is_accounted_for

    def test_every_handoff_is_accounted_for():
        assert_every_site_is_accounted_for()

It is static and standard-library only: it imports neither the application nor
its tests, so it needs no framework, settings or database. See
:mod:`.scan` for the rules and :mod:`.config` for ``[tool.due-work-harness]``.
"""

from pathlib import Path

from due_work_harness.coverage.config import CoverageConfig, load_config
from due_work_harness.coverage.scan import (
    CoverageReport,
    Disposition,
    Site,
    baseline_growth,
    production_sites,
    scan,
    unaccounted_baseline,
)
from due_work_harness.coverage.sites import BUILT_IN, SiteKind, installed_kinds


def assert_every_site_is_accounted_for(root: Path | str = ".") -> CoverageReport:
    """Scan the project at ``root`` (its ``pyproject.toml`` configures the scan); fail on any problem."""
    report = scan(load_config(root))
    report.raise_for_problems()
    return report


__all__ = [
    "BUILT_IN",
    "CoverageConfig",
    "CoverageReport",
    "Disposition",
    "Site",
    "SiteKind",
    "assert_every_site_is_accounted_for",
    "baseline_growth",
    "installed_kinds",
    "load_config",
    "production_sites",
    "scan",
    "unaccounted_baseline",
]
