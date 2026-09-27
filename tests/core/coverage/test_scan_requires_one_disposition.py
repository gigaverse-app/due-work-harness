"""
Dispositions: every site has exactly one, and every declaration is well-formed.

Each rule is shown passing on a conforming project and failing, with its own
message, on the project that breaks it.
"""

from pathlib import Path

import pytest

from due_work_harness.coverage import scan, unaccounted_baseline

from .builders import write_project

ORDERS = """
    from django.db import transaction

    class OrderService:
        def place(self):
            transaction.on_commit(lambda: None)
"""

CONTRACT_MODULE = """
    from due_work_harness import DueWorkContract, DueWorkSource, due_work_contract_suite
    from shop.orders import OrderService

    ORDERS = DueWorkContract(name="orders")

    @due_work_contract_suite(ORDERS, covers=({source},))
    class TestOrdersDueWork:
        pass
"""

EXEMPTION_MODULE = """
    from due_work_harness import DueWorkSource, exempt_due_work_suite
    from shop.orders import OrderService

    @exempt_due_work_suite(
        DueWorkSource(OrderService.place),
        reason={reason},
        {prove}
    )
    class TestOrdersExemption:
        pass
"""


def _problems(root: Path, files: dict[str, str], extra: str = "") -> list[str]:
    return scan(write_project(root, {"shop/orders.py": ORDERS, **files}, sites='["django"]', extra=extra)).problems


def test_a_covered_site_is_accounted_for(tmp_path: Path) -> None:
    covered = CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place)")
    assert _problems(tmp_path, {"tests/test_orders_due_work.py": covered}) == []


def test_a_site_with_no_disposition_fails(tmp_path: Path) -> None:
    (problem,) = _problems(tmp_path, {})
    assert "shop.orders.OrderService.place (shop/orders.py:6, django) hands work off with no disposition" in problem


def test_a_disposition_naming_a_function_with_no_site_is_stale(tmp_path: Path) -> None:
    covered = CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place), DueWorkSource(OrderService.refund)")
    orders = ORDERS + "\n        def refund(self):\n            pass\n"
    problems = scan(
        write_project(
            tmp_path, {"shop/orders.py": orders, "tests/test_orders_due_work.py": covered}, sites='["django"]'
        )
    ).problems
    assert problems == [
        "shop.orders.OrderService.refund is covered (tests/test_orders_due_work.py::TestOrdersDueWork) but has no "
        "handoff site: remove the stale disposition, or point it at the function that now hands the work off"
    ]


def test_a_changed_number_of_sites_is_drift(tmp_path: Path) -> None:
    covered = CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place, sites=2)")
    (problem,) = _problems(tmp_path, {"tests/test_orders_due_work.py": covered})
    assert "has 1 handoff site(s) but its disposition (covered" in problem and "accounts for 2" in problem


def test_two_dispositions_for_one_site_fail(tmp_path: Path) -> None:
    covered = CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place)")
    (problem,) = _problems(
        tmp_path,
        {"tests/test_orders_due_work.py": covered},
        extra='[tool.due-work-harness.baseline]\n"shop.orders.OrderService.place" = 1',
    )
    assert problem.startswith("shop.orders.OrderService.place has more than one disposition: covered in")


def test_a_proven_exemption_accounts_for_a_site(tmp_path: Path) -> None:
    exemption = EXEMPTION_MODULE.format(
        reason='"the next request re-derives the order summary from the database"', prove="prove=lambda: None,"
    )
    assert _problems(tmp_path, {"tests/test_orders_exemption.py": exemption}) == []


@pytest.mark.parametrize(
    ("reason", "prove", "defect"),
    [
        ('"the next request re-derives the order summary"', "", "an exemption must carry prove="),
        ('"fine"', "prove=lambda: None,", "the exemption's reason is too thin"),
        ("REASON", "prove=lambda: None,", "reason= must be a literal string"),
    ],
    ids=["no-proof", "thin-reason", "computed-reason"],
)
def test_an_exemption_needs_a_literal_reason_and_a_proof(tmp_path: Path, reason: str, prove: str, defect: str) -> None:
    exemption = EXEMPTION_MODULE.format(reason=reason, prove=prove)
    problems = _problems(tmp_path, {"tests/test_orders_exemption.py": exemption})
    assert any(defect in problem for problem in problems), problems
    assert any("hands work off with no disposition" in problem for problem in problems)


@pytest.mark.parametrize(
    ("module", "defect"),
    [
        (
            CONTRACT_MODULE.format(source='"shop.orders.OrderService.place"'),
            "entries must be typed DueWorkSource(callable) values",
        ),
        (
            CONTRACT_MODULE.format(source="DueWorkSource(place)"),
            "DueWorkSource must receive an imported production callable",
        ),
        (
            CONTRACT_MODULE.replace("covers=({source},)", "covers=SOURCES").format(),
            "covers= must be one inline tuple",
        ),
        (
            CONTRACT_MODULE.replace('ORDERS = DueWorkContract(name="orders")', "ORDERS = make_contract()").format(
                source="DueWorkSource(OrderService.place)"
            ),
            "which this module does not build as DueWorkContract",
        ),
        (
            CONTRACT_MODULE.replace("class TestOrdersDueWork", "class OrdersDueWork").format(
                source="DueWorkSource(OrderService.place)"
            ),
            "is not a collected Test* class",
        ),
    ],
    ids=["path-string", "unimported-callable", "covers-not-inline", "contract-not-built-here", "not-collected"],
)
def test_a_malformed_declaration_is_refused(tmp_path: Path, module: str, defect: str) -> None:
    problems = _problems(tmp_path, {"tests/test_orders_due_work.py": module})
    assert any(defect in problem for problem in problems), problems


def test_a_baseline_accounts_for_a_legacy_site(tmp_path: Path) -> None:
    extra = '[tool.due-work-harness.baseline]\n"shop.orders.OrderService.place" = 1'
    assert _problems(tmp_path, {}, extra=extra) == []


def test_the_adoption_baseline_lists_every_unaccounted_site(tmp_path: Path) -> None:
    report = scan(write_project(tmp_path, {"shop/orders.py": ORDERS}, sites='["django"]'))
    assert unaccounted_baseline(report) == {"shop.orders.OrderService.place": 1}


def test_a_forwarding_helper_must_declare_its_own_sites(tmp_path: Path) -> None:
    shared = "from django.db import transaction\n\ndef after_commit(callback):\n    transaction.on_commit(callback)\n"
    problems = scan(
        write_project(
            tmp_path,
            {"shop/orders.py": ORDERS, "shop/shared.py": shared},
            sites='["django"]',
            extra='bridges = { "shop.shared.after_commit" = 2 }\n[tool.due-work-harness.baseline]\n'
            '"shop.orders.OrderService.place" = 1',
        )
    ).problems
    assert problems == [
        "shop.shared.after_commit has 1 handoff site(s) but its disposition (bridge, pyproject.toml "
        "[tool.due-work-harness] bridges) accounts for 2: review it and update sites="
    ]
