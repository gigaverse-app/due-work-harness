"""
Dispositions: every site has exactly one, and every declaration is well-formed.

Each rule is shown passing on a conforming project and failing, with its own
message, on the project that breaks it.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from pytest_obligation.coverage import scan, unaccounted_baseline

from .builders import write_project

ORDERS = """
    from django.db import transaction

    class OrderService:
        def place(self):
            transaction.on_commit(lambda: None)
"""

CONTRACT_MODULE = """
    from pytest_obligation import ObligationContract, DueWorkSource, due_work_contract_suite
    from shop.orders import OrderService

    ORDERS = ObligationContract(name="orders")

    @due_work_contract_suite(ORDERS, covers=({source},))
    class TestOrdersDueWork:
        pass
"""

EXEMPTION_MODULE = """
    from pytest_obligation import DueWorkSource, LossIsAbsorbedElsewhere, exempt_due_work_suite
    from shop.orders import OrderService

    @exempt_due_work_suite(
        DueWorkSource(OrderService.place),
        reason={reason},
        {prove}
    )
    class TestOrdersExemption:
        pass
"""


PROOF = (
    "prove=LossIsAbsorbedElsewhere(strand=OrderService.place, observe=OrderService.place, absorb=OrderService.place),"
)
REASON = '"the next request re-derives the order summary from the database"'


def _problems(root: Path, files: dict[str, str], extra: str = "") -> list[str]:
    return scan(write_project(root, {"shop/orders.py": ORDERS, **files}, extra=extra)).problems


def test_a_covered_site_is_accounted_for(tmp_path: Path) -> None:
    covered = CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place)")
    assert _problems(tmp_path, {"tests/test_orders_due_work.py": covered}) == []


@pytest.mark.parametrize(
    "package,constructor", [("pytest_obligation", "ObligationContract"), ("due_work_harness", "DueWorkContract")]
)
def test_both_namespaces_account_for_the_same_production_site(tmp_path: Path, package: str, constructor: str) -> None:
    covered = CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place)")
    covered = covered.replace("pytest_obligation", package).replace("ObligationContract", constructor)
    assert _problems(tmp_path, {"tests/test_orders_due_work.py": covered}) == []


def test_a_site_with_no_disposition_fails(tmp_path: Path) -> None:
    (problem,) = _problems(tmp_path, {})
    assert "shop.orders.OrderService.place (shop/orders.py:6, django) hands work off with no disposition" in problem


def test_a_disposition_naming_a_function_with_no_site_is_stale(tmp_path: Path) -> None:
    covered = CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place), DueWorkSource(OrderService.refund)")
    orders = ORDERS + "\n        def refund(self):\n            pass\n"
    problems = scan(
        write_project(tmp_path, {"shop/orders.py": orders, "tests/test_orders_due_work.py": covered})
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
    exemption = EXEMPTION_MODULE.format(reason=REASON, prove=PROOF)
    assert _problems(tmp_path, {"tests/test_orders_exemption.py": exemption}) == []


def test_a_proof_bound_to_a_module_level_probe_is_a_harness_proof(tmp_path: Path) -> None:
    probe = "CHECK = LossIsAbsorbedElsewhere(strand=print, observe=print, absorb=print)\n\n    @exempt_due_work_suite("
    exemption = EXEMPTION_MODULE.replace("@exempt_due_work_suite(", probe).format(reason=REASON, prove="prove=CHECK,")
    assert _problems(tmp_path, {"tests/test_orders_exemption.py": exemption}) == []


@pytest.mark.parametrize(
    ("reason", "prove", "defect"),
    [
        (REASON, "", "an exemption must carry prove="),
        (REASON, "prove=lambda: None,", "prove= must be a harness probe"),
        (REASON, "prove=OrderService,", None),
        ('"fine"', PROOF, "the exemption's reason is too thin"),
        ("REASON", PROOF, "reason= must be a literal string"),
    ],
    ids=["no-proof", "test-authored-proof", "production-proof", "thin-reason", "computed-reason"],
)
def test_an_exemption_needs_a_literal_reason_and_a_proof_the_test_did_not_write(
    tmp_path: Path, reason: str, prove: str, defect: str | None
) -> None:
    exemption = EXEMPTION_MODULE.format(reason=reason, prove=prove)
    problems = _problems(tmp_path, {"tests/test_orders_exemption.py": exemption})
    if defect is None:
        assert problems == []
        return
    assert any(defect in problem for problem in problems), problems
    assert any("hands work off with no disposition" in problem for problem in problems)


def test_a_proof_defined_in_the_test_module_is_test_authored(tmp_path: Path) -> None:
    local = "def absorbed():\n        pass\n\n    @exempt_due_work_suite("
    exemption = EXEMPTION_MODULE.replace("@exempt_due_work_suite(", local).format(
        reason=REASON, prove="prove=absorbed,"
    )
    problems = _problems(tmp_path, {"tests/test_orders_exemption.py": exemption})
    assert any("prove= must be a harness probe" in problem for problem in problems), problems


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
            CONTRACT_MODULE.replace('ORDERS = ObligationContract(name="orders")', "ORDERS = make_contract()").format(
                source="DueWorkSource(OrderService.place)"
            ),
            "which this module does not build as pytest_obligation's ObligationContract",
        ),
        (
            CONTRACT_MODULE.replace("class TestOrdersDueWork", "class OrdersDueWork").format(
                source="DueWorkSource(OrderService.place)"
            ),
            "is not a collected Test* class",
        ),
        (
            CONTRACT_MODULE.replace(
                "from pytest_obligation import ObligationContract, DueWorkSource, due_work_contract_suite",
                "from pytest_obligation import ObligationContract, DueWorkSource\n    from tests.fakes import due_work_contract_suite",
            ).format(source="DueWorkSource(OrderService.place)"),
            "due_work_contract_suite is not pytest_obligation's",
        ),
        (
            CONTRACT_MODULE.replace(
                "from pytest_obligation import ObligationContract, DueWorkSource, due_work_contract_suite",
                "from pytest_obligation import ObligationContract, due_work_contract_suite\n    from tests.fakes import DueWorkSource",
            ).format(source="DueWorkSource(OrderService.place)"),
            "DueWorkSource must be pytest_obligation's",
        ),
        (
            CONTRACT_MODULE.replace(
                "from pytest_obligation import ObligationContract, DueWorkSource, due_work_contract_suite",
                "from pytest_obligation import DueWorkSource, due_work_contract_suite\n    from tests.fakes import ObligationContract",
            ).format(source="DueWorkSource(OrderService.place)"),
            "which this module does not build as pytest_obligation's ObligationContract",
        ),
    ],
    ids=[
        "path-string",
        "unimported-callable",
        "covers-not-inline",
        "contract-not-built-here",
        "not-collected",
        "spoofed-suite",
        "spoofed-source",
        "spoofed-contract",
    ],
)
def test_a_malformed_declaration_is_refused(tmp_path: Path, module: str, defect: str) -> None:
    problems = _problems(tmp_path, {"tests/test_orders_due_work.py": module})
    assert any(defect in problem for problem in problems), problems


def test_a_baseline_accounts_for_a_legacy_site(tmp_path: Path) -> None:
    extra = '[tool.due-work-harness.baseline]\n"shop.orders.OrderService.place" = 1'
    assert _problems(tmp_path, {}, extra=extra) == []


def test_the_adoption_baseline_lists_every_unaccounted_site(tmp_path: Path) -> None:
    report = scan(write_project(tmp_path, {"shop/orders.py": ORDERS}))
    assert unaccounted_baseline(report) == {"shop.orders.OrderService.place": 1}


def test_a_forwarding_helper_must_declare_its_own_sites(tmp_path: Path) -> None:
    shared = "from django.db import transaction\n\ndef after_commit(callback):\n    transaction.on_commit(callback)\n"
    problems = scan(
        write_project(
            tmp_path,
            {"shop/orders.py": ORDERS, "shop/shared.py": shared},
            extra='bridges = { "shop.shared.after_commit" = 2 }\n[tool.due-work-harness.baseline]\n'
            '"shop.orders.OrderService.place" = 1',
        )
    ).problems
    assert problems == [
        "shop.shared.after_commit has 1 handoff site(s) but its disposition (bridge, pyproject.toml "
        "[tool.due-work-harness] bridges) accounts for 2: review it and update sites="
    ]


def _nest(module: str) -> str:
    nested = module.replace("@due_work_contract_suite", "def make():\n        @due_work_contract_suite")
    return nested.replace(
        "    class TestOrdersDueWork:\n        pass", "        class TestOrdersDueWork:\n            pass"
    )


@pytest.mark.parametrize(
    ("edit", "defect"),
    [
        (_nest, "is not a module-level class"),
        (lambda module: module + "\n    class TestOrdersDueWork:\n        pass\n", "is rebound on line"),
        (lambda module: module + "\n    TestOrdersDueWork = None\n", "is rebound on line"),
        (
            lambda module: module.replace(
                "@due_work_contract_suite", "@pytest.mark.skip\n    @due_work_contract_suite"
            ),
            "carries pytest.mark.skip",
        ),
        (
            lambda module: module.replace(
                "@due_work_contract_suite", "@pytest.mark.skipif(True, reason='x')\n    @due_work_contract_suite"
            ),
            "carries pytest.mark.skipif",
        ),
        (
            lambda module: module.replace("        pass", "        pytestmark = pytest.mark.xfail"),
            "carries pytest.mark.xfail",
        ),
        (lambda module: module + "\n    pytestmark = [pytest.mark.skip]\n", "carries pytest.mark.skip"),
        (lambda module: module.replace("        pass", "        __test__ = False"), "sets __test__"),
    ],
    ids=[
        "nested-class",
        "shadowed-by-class",
        "shadowed-by-assignment",
        "skip-mark",
        "skipif-mark",
        "class-pytestmark",
        "module-pytestmark",
        "dunder-test",
    ],
)
def test_a_declaration_that_would_not_run_does_not_count(
    tmp_path: Path, edit: Callable[[str], str], defect: str
) -> None:
    module = "\n    import pytest\n" + CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place)")
    problems = _problems(tmp_path, {"tests/test_orders_due_work.py": edit(module)})
    assert any(defect in problem for problem in problems), problems
    assert any("hands work off with no disposition" in problem for problem in problems), problems


def test_the_same_declaration_with_a_harmless_mark_counts(tmp_path: Path) -> None:
    module = "\n    import pytest\n" + CONTRACT_MODULE.format(source="DueWorkSource(OrderService.place)")
    module = module.replace("@due_work_contract_suite", "@pytest.mark.slow\n    @due_work_contract_suite")
    assert _problems(tmp_path, {"tests/test_orders_due_work.py": module}) == []


def test_the_harness_imported_as_a_module_is_the_harness(tmp_path: Path) -> None:
    module = """
        import pytest_obligation as harness
        from shop import orders

        ORDERS = harness.ObligationContract(name="orders")

        @harness.due_work_contract_suite(ORDERS, covers=(harness.DueWorkSource(orders.OrderService.place),))
        class TestOrdersDueWork:
            pass
    """
    assert _problems(tmp_path, {"tests/test_orders_due_work.py": module}) == []


def test_a_source_named_through_a_package_re_export_is_the_defining_function(tmp_path: Path) -> None:
    module = CONTRACT_MODULE.replace("from shop.orders import OrderService", "from shop import OrderService")
    files = {
        "shop/__init__.py": "from shop.orders import OrderService\n",
        "shop/orders.py": ORDERS,
        "tests/test_orders_due_work.py": module.format(source="DueWorkSource(OrderService.place)"),
    }
    assert scan(write_project(tmp_path, files)).problems == []


def test_a_site_outside_any_function_names_its_fix(tmp_path: Path) -> None:
    orders = "from django.db import transaction\n\ntransaction.on_commit(print)\n"
    (problem,) = scan(write_project(tmp_path, {"shop/orders.py": orders})).problems
    assert problem.startswith("shop.orders.<module> (shop/orders.py:3, django) hands work off outside any function")


@pytest.mark.parametrize(
    ("exclude", "hidden"),
    [('["build"]', "shop/build"), ('["legacy_*.py"]', "shop/legacy_orders.py")],
    ids=["directory", "file"],
)
def test_exclude_may_not_hide_production_code(tmp_path: Path, exclude: str, hidden: str) -> None:
    files = {
        "shop/build/pipeline.py": ORDERS,
        "shop/legacy_orders.py": ORDERS,
        "build/generated.py": ORDERS,
        "shop/tests/fixtures.py": ORDERS,
    }
    report = scan(write_project(tmp_path, files, extra=f"exclude = {exclude}"))
    assert f"{hidden} holds production code but `exclude` hides it from the scan: narrow the pattern" in report.problems
    assert not any("build/generated.py" in problem or "shop/tests" in problem for problem in report.problems)


def test_a_built_in_skip_never_hides_production_code(tmp_path: Path) -> None:
    report = scan(write_project(tmp_path, {"shop/build/pipeline.py": ORDERS, "build/generated.py": ORDERS}))
    assert list(report.sites) == ["shop.build.pipeline.OrderService.place"]


def test_an_excluded_directory_without_source_hides_nothing(tmp_path: Path) -> None:
    files = {"shop/orders.py": ORDERS, "shop/__pycache__/orders.cpython-312.pyc": "bytecode"}
    report = scan(write_project(tmp_path, files, extra='exclude = ["__pycache__"]'))
    assert not any("holds production code" in problem for problem in report.problems), report.problems
