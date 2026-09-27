"""The coverage scan on Saleor at the pinned commit: every handoff it finds, attributed to its function."""

from collections import Counter

from conftest import SALEOR

from due_work_harness.coverage.config import CoverageConfig
from due_work_harness.coverage.scan import scan

REPORT = scan(CoverageConfig(root=SALEOR, production_packages=("saleor",), source_roots=(".",), test_paths=(".",)))


def test_the_scan_finds_the_handoffs_the_crash_histories_prove_losable() -> None:
    sites = {name: [site.kind for site in found] for name, found in REPORT.sites.items()}
    # The two on_commit callbacks behind findings 2 and 3.
    assert sites["saleor.checkout.complete_checkout._post_create_order_actions"] == ["django", "django"]


def test_every_handoff_in_saleor_is_found_and_none_is_accounted_for() -> None:
    kinds = Counter(site.kind for found in REPORT.sites.values() for site in found)
    assert REPORT.kinds == ["django", "celery"]
    assert kinds == {"celery": 115, "django": 22}
    assert len(REPORT.unaccounted) == len(REPORT.sites) == 124
