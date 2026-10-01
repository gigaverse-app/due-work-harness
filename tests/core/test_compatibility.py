"""Old and new adopters must execute one canonical harness implementation."""

import importlib
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

CANONICAL = "pytest_obligation"
LEGACY = "due_work_harness"


def test_old_contract_is_the_canonical_class() -> None:
    canonical = importlib.import_module(CANONICAL)
    legacy = importlib.import_module(LEGACY)
    contract = importlib.import_module(f"{LEGACY}.contract")
    assert legacy.DueWorkContract is canonical.ObligationContract
    assert contract.DueWorkContract is canonical.ObligationContract
    assert canonical.ObligationContract.__module__ == f"{CANONICAL}.contract"
    assert canonical.ObligationContract.__name__ == "ObligationContract"
    assert pickle.loads(pickle.dumps(legacy.DueWorkContract)) is canonical.ObligationContract
    assert pickle.loads(b"cdue_work_harness.contract\nDueWorkContract\n.") is canonical.ObligationContract
    assert legacy.DueWorkContractDesignError is canonical.ObligationContractDesignError
    assert (
        pickle.loads(b"cdue_work_harness.models\nDueWorkContractDesignError\n.")
        is canonical.ObligationContractDesignError
    )


def test_every_legacy_core_submodule_resolves_to_one_implementation() -> None:
    package = importlib.import_module(CANONICAL)
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if "integrations" in relative.parts or path.name == "__init__.py":
            continue
        module = ".".join(relative.with_suffix("").parts)
        legacy_module = importlib.import_module(f"{LEGACY}.{module}")
        canonical_module = importlib.import_module(f"{CANONICAL}.{module}")
        assert legacy_module is canonical_module
        assert canonical_module.__spec__ is not None
        assert canonical_module.__spec__.name == f"{CANONICAL}.{module}"
        assert canonical_module.__package__ == canonical_module.__spec__.parent


@pytest.mark.parametrize(
    "module", ["host", "models", "recording", "coverage.scan", "profiles.catalog", "interleavings.engine.runner"]
)
def test_submodule_aliases_share_identity(module: str) -> None:
    assert importlib.import_module(f"{LEGACY}.{module}") is importlib.import_module(f"{CANONICAL}.{module}")


def test_legacy_and_canonical_hosts_share_active_context() -> None:
    canonical = importlib.import_module(f"{CANONICAL}.host")
    legacy = importlib.import_module(f"{LEGACY}.host")
    host = canonical.Host()
    with legacy.hosted(host):
        assert canonical.current_host() is host
        assert legacy.current_host() is host


def test_canonical_import_does_not_load_compatibility_or_frameworks() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pytest_obligation, sys; assert not any(n == 'due_work_harness' or n.startswith('due_work_harness.') for n in sys.modules); assert not any(n in sys.modules for n in ('django', 'celery', 'pymongo', 'prefect', 'aiokafka'))",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("package", [CANONICAL, LEGACY])
def test_module_cli_remains_executable(package: str) -> None:
    result = subprocess.run([sys.executable, "-m", f"{package}.coverage.cli", "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "in-transaction" in result.stdout


@pytest.mark.parametrize("package,constructor", [(CANONICAL, "ObligationContract"), (LEGACY, "DueWorkContract")])
def test_adopters_generate_and_execute_the_same_suite(
    pytester: pytest.Pytester, package: str, constructor: str
) -> None:
    pytester.makepyfile(f"""
from {package} import {constructor}, NotApplicable, Profile, due_work_contract_suite
CONTRACT = {constructor}(name="compatibility", profiles={{p: NotApplicable("Import compatibility control.") for p in Profile}})
@due_work_contract_suite(CONTRACT)
class TestCompatibility:
    pass
""")
    result = pytester.runpytest_subprocess("-q", "-p", "due_work_harness", "--due-work-require-assessed")
    result.assert_outcomes(passed=10)
