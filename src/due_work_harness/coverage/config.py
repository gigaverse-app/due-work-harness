"""
Coverage configuration: ``[tool.due-work-harness]`` in the project's ``pyproject.toml``.

::

    [tool.due-work-harness]
    production-packages = ["myapp"]     # dotted prefixes of the code under test (required)
    source-roots = ["src", "."]         # where module names start; the first root containing a file wins
    test-paths = ["."]                  # where contract and exemption suites are looked for
    exclude = [".venv*", "node_modules", "build", "dist"]
    sites = ["django", "celery"]        # default: every installed integration
    bridges = { "myapp.shared.after_commit" = 1 }   # forwarding helpers and their own site count
    bridge-methods = ["request_progress"]           # forwarding methods, matched by name

    [tool.due-work-harness.baseline]
    # Sites that predate adoption and have no disposition yet. It only shrinks:
    # `due-work-harness check --base-ref <ref>` refuses entries added since <ref>.
    "myapp.legacy.send_welcome_email" = 1
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from due_work_harness.coverage.sites import SiteKind, installed_kinds, resolve_kinds

DEFAULT_EXCLUDE = (".venv*", "venv", "node_modules", "build", "dist", ".git", "__pycache__", ".tox", ".nox")


@dataclass(frozen=True)
class CoverageConfig:
    root: Path
    production_packages: tuple[str, ...]
    source_roots: tuple[str, ...] = ("src", ".")
    test_paths: tuple[str, ...] = (".",)
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    kinds: tuple[SiteKind, ...] = field(default_factory=installed_kinds)
    bridges: Mapping[str, int] = field(default_factory=dict)
    bridge_methods: frozenset[str] = frozenset()
    baseline: Mapping[str, int] = field(default_factory=dict)


def _table(pyproject: Path) -> dict[str, Any] | None:
    if not pyproject.is_file():
        return None
    with pyproject.open("rb") as handle:
        document = tomllib.load(handle)
    return document.get("tool", {}).get("due-work-harness")


def _strings(table: Mapping[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = table.get(key, default)
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"[tool.due-work-harness] {key} must be a list of strings")
    return tuple(value)


def _counts(table: Mapping[str, Any], key: str) -> dict[str, int]:
    value = table.get(key, {})
    if not isinstance(value, Mapping) or not all(
        isinstance(name, str) and isinstance(count, int) and count >= 1 for name, count in value.items()
    ):
        raise ValueError(f"[tool.due-work-harness] {key} must map qualified callables to site counts of at least 1")
    return dict(value)


def baseline_from(pyproject_text: str) -> dict[str, int] | None:
    """The baseline in a pyproject document, or ``None`` when it has no due-work-harness table."""
    table = tomllib.loads(pyproject_text).get("tool", {}).get("due-work-harness")
    return None if table is None else _counts(table, "baseline")


def load_config(root: Path | str = ".") -> CoverageConfig:
    """Read the configuration from ``<root>/pyproject.toml``."""
    root = Path(root).resolve()
    table = _table(root / "pyproject.toml")
    if table is None:
        raise ValueError(f"{root / 'pyproject.toml'} has no [tool.due-work-harness] table")
    packages = _strings(table, "production-packages", ())
    if not packages:
        raise ValueError("[tool.due-work-harness] production-packages must name the code under test")
    kinds = table.get("sites")
    return CoverageConfig(
        root=root,
        production_packages=packages,
        source_roots=_strings(table, "source-roots", ("src", ".")),
        test_paths=_strings(table, "test-paths", (".",)),
        exclude=_strings(table, "exclude", DEFAULT_EXCLUDE),
        kinds=resolve_kinds(None if kinds is None else _strings(table, "sites", ())),
        bridges=_counts(table, "bridges"),
        bridge_methods=frozenset(_strings(table, "bridge-methods", ())),
        baseline=_counts(table, "baseline"),
    )
