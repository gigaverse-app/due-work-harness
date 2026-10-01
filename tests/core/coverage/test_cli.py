"""The command line: exit codes, the inventory, the adoption baseline, and a baseline that only shrinks."""

import subprocess
from pathlib import Path

import pytest

from pytest_obligation.coverage.cli import main

from .builders import write_project

ORDERS = "from django.db import transaction\n\ndef place():\n    transaction.on_commit(print)\n"
BASELINE = '[tool.due-work-harness.baseline]\n"shop.orders.place" = 1'


def test_check_fails_on_an_unaccounted_site(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_project(tmp_path, {"shop/orders.py": ORDERS})
    assert main(["check", "--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "1 problem(s) across 1 handoff site(s) [django]" in output
    assert "shop.orders.place (shop/orders.py:4, django)" in output


def test_check_passes_when_every_site_is_accounted_for(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_project(tmp_path, {"shop/orders.py": ORDERS}, extra=BASELINE)
    assert main(["check", "--root", str(tmp_path)]) == 0
    assert "every one of 1 handoff site(s) in 1 function(s) is accounted for [django]" in capsys.readouterr().out


def test_sites_lists_the_inventory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_project(tmp_path, {"shop/orders.py": ORDERS})
    assert main(["sites", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "shop/orders.py:4\tdjango\tshop.orders.place\tUNACCOUNTED"


def test_baseline_prints_a_table_for_first_adoption(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_project(tmp_path, {"shop/orders.py": ORDERS})
    assert main(["baseline", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == BASELINE


def test_a_missing_configuration_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    assert main(["check", "--root", str(tmp_path)]) == 2
    assert "has no [tool.due-work-harness] table" in capsys.readouterr().err


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


TWO_SITES = "from django.db import transaction\n\ndef place():\n    transaction.on_commit(print)\n    transaction.on_commit(print)\n"


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "change")


@pytest.mark.parametrize(
    ("before", "after", "orders_after", "growth"),
    [
        ("", '"shop.orders.place" = 1', ORDERS, "(0 -> 1 site(s))"),
        ('"shop.orders.place" = 1', '"shop.orders.place" = 2', TWO_SITES, "(1 -> 2 site(s))"),
        ('"shop.orders.place" = 2', '"shop.orders.place" = 1', ORDERS, None),
    ],
    ids=["entry-added", "count-grown", "count-shrunk"],
)
def test_the_baseline_only_shrinks_against_a_base_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], before: str, after: str, orders_after: str, growth: str | None
) -> None:
    orders_before = TWO_SITES if "= 2" in before else ORDERS
    write_project(tmp_path, {"shop/orders.py": orders_before}, extra=f"[tool.due-work-harness.baseline]\n{before}")
    _git(tmp_path, "init", "-q")
    _commit(tmp_path)
    write_project(tmp_path, {"shop/orders.py": orders_after}, extra=f"[tool.due-work-harness.baseline]\n{after}")
    assert main(["check", "--root", str(tmp_path)]) == 0
    capsys.readouterr()
    status = main(["check", "--root", str(tmp_path), "--base-ref", "HEAD"])
    output = capsys.readouterr().out
    if growth is None:
        assert status == 0, output
    else:
        assert status == 1
        assert f"shop.orders.place grew in the baseline since HEAD {growth}; the baseline only shrinks" in output


def test_first_adoption_is_not_growth(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'shop'\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "before")
    write_project(tmp_path, {"shop/orders.py": ORDERS}, extra=BASELINE)
    assert main(["check", "--root", str(tmp_path), "--base-ref", "HEAD"]) == 0


def test_a_base_ref_that_is_not_fetched_is_an_error_not_a_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_project(tmp_path, {"shop/orders.py": ORDERS}, extra=BASELINE)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "adopt")
    assert main(["check", "--root", str(tmp_path), "--base-ref", "0123456789abcdef0123456789abcdef01234567"]) == 2
    assert "is not in this repository" in capsys.readouterr().err
