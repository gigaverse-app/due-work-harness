"""Throwaway projects for the coverage self-tests: a pyproject.toml plus source files."""

from pathlib import Path
from textwrap import dedent

from pytest_obligation.coverage import CoverageConfig, load_config

PYPROJECT = """
[tool.due-work-harness]
production-packages = ["shop"]
sites = {sites}
{extra}
"""


def write_project(
    root: Path,
    files: dict[str, str],
    *,
    sites: str = "[]",
    extra: str = "",
) -> CoverageConfig:
    (root / "pyproject.toml").write_text(dedent(PYPROJECT).format(sites=sites, extra=dedent(extra)), encoding="utf-8")
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text), encoding="utf-8")
    for package in {Path(relative).parts[0] for relative in files if relative.startswith("shop/")}:
        init = root / package / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    return load_config(root)
