"""
Coverage configuration: ``[tool.due-work-harness]`` in the project's ``pyproject.toml``.

::

    [tool.due-work-harness]
    production-packages = ["myapp"]     # dotted prefixes of the code under test (required)
    source-roots = ["src", "."]         # where module names start; the first root containing a file wins;
                                        # a root may lie outside the project ("../vendor/app")
    test-paths = ["."]                  # where contract and exemption suites are looked for
    exclude = ["migrations"]            # added to the built-in skips; may never hide production code
    sites = ["celery"]                  # kinds to add to those detected from production's imports
    bridges = { "myapp.shared.after_commit" = 1 }   # forwarding helpers and their own site count
    bridge-methods = ["request_progress"]           # forwarding methods, matched by name

    [tool.due-work-harness.baseline]
    # Sites that predate adoption and have no disposition yet. It only shrinks:
    # `due-work-harness check --base-ref <ref>` refuses entries added, or grown, since <ref>.
    "myapp.legacy.send_welcome_email" = 1
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field

from due_work_harness.coverage.sites import SiteKind, resolve_kinds
from due_work_harness.models import HarnessModel

#: Always skipped, except where production code lives (a package named ``build`` is still scanned).
DEFAULT_EXCLUDE = (".venv*", "venv", "node_modules", "build", "dist", ".git", "__pycache__", ".tox", ".nox")


class CoverageConfig(HarnessModel):
    root: Path
    production_packages: tuple[str, ...]
    source_roots: tuple[str, ...] = ("src", ".")
    test_paths: tuple[str, ...] = (".",)
    #: Name patterns to skip in addition to :data:`DEFAULT_EXCLUDE`.
    exclude: tuple[str, ...] = ()
    #: Kinds scanned in addition to those production's imports enable.
    kinds: tuple[SiteKind, ...] = ()
    bridges: Mapping[str, int] = Field(default_factory=dict)
    bridge_methods: frozenset[str] = frozenset()
    baseline: Mapping[str, int] = Field(default_factory=dict)

    def is_production(self, qualified: str) -> bool:
        """Whether a dotted name lies inside one of the production packages."""
        return any(qualified == package or qualified.startswith(f"{package}.") for package in self.production_packages)


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
    return CoverageConfig(
        root=root,
        production_packages=packages,
        source_roots=_strings(table, "source-roots", ("src", ".")),
        test_paths=_strings(table, "test-paths", (".",)),
        exclude=_strings(table, "exclude", ()),
        kinds=resolve_kinds(_strings(table, "sites", ())),
        bridges=_counts(table, "bridges"),
        bridge_methods=frozenset(_strings(table, "bridge-methods", ())),
        baseline=_counts(table, "baseline"),
    )
