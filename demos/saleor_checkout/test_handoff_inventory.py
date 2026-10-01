"""
Every handoff in Saleor is accounted for, as ``due-work-harness check`` requires.

``CHECKOUT_AS_SHIPPED`` covers the two ``on_commit`` handoffs behind the
checkout findings; the baseline in ``pyproject.toml`` holds the rest, and only
shrinks as contracts cover them.
"""

from collections import Counter
from pathlib import Path

from pytest_obligation.coverage import assert_every_site_is_accounted_for

HERE = Path(__file__).resolve().parent


def test_every_handoff_in_saleor_is_accounted_for() -> None:
    report = assert_every_site_is_accounted_for(HERE)
    assert report.kinds == ["django", "celery"]
    assert Counter(site.kind for found in report.sites.values() for site in found) == {"celery": 115, "django": 22}
    covered = {name for name, disposition in report.dispositions.items() if disposition.how == "covered"}
    assert covered == {"saleor.checkout.complete_checkout._post_create_order_actions"}
    assert len(report.dispositions) == len(report.sites) == 124
