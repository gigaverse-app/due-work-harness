"""
The project as the coverage check reads it: its modules, and the names they bind.

Nothing is imported. Modules are found under the configured source roots,
parsed on demand, and names are resolved statically through imports, relative
imports and re-exports, so a function has one qualified name however it is
reached.
"""

import ast
import fnmatch
import os
import sys
from collections.abc import Iterator, Mapping
from functools import cached_property
from pathlib import Path

from due_work_harness.binding import is_test_path
from due_work_harness.coverage.config import DEFAULT_EXCLUDE, CoverageConfig
from due_work_harness.models import HarnessModel


class Module(HarnessModel):
    name: str
    path: Path
    relative: str
    package: bool


def is_excluded(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def module_name(config: CoverageConfig, path: Path) -> str | None:
    """The dotted name of a file or directory under the first source root containing it."""
    for root in config.source_roots:
        base = (config.root / root).resolve()
        if base in path.parents:
            parts = list(path.relative_to(base).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            return ".".join(parts)
    return None


class Project:
    """The project's non-test modules, and the production code among them, found once per scan."""

    def __init__(self, config: CoverageConfig) -> None:
        self.config = config
        self.modules: dict[str, Module] = {}
        #: Excluded paths that hold production code, which the scan would otherwise never see.
        self.hidden: list[str] = []
        #: Files the running Python cannot parse; their sites are unknown, so each is a problem.
        self.unparsable: list[str] = []
        self._trees: dict[Path, ast.Module] = {}
        self._imports: dict[str, dict[str, str]] = {}
        for root in config.source_roots:
            base = (config.root / root).resolve()
            if base.is_dir():
                for path in self._walk(base):
                    self._add(path)

    def _relative(self, path: Path) -> Path:
        return path.relative_to(self.config.root)

    def _holds_production(self, path: Path) -> bool:
        """Whether a file, or a directory with Python source in it, is (or contains) production code."""
        if is_test_path(self._relative(path)):
            return False
        name = module_name(self.config, path)
        if name is None or not (
            self.config.is_production(name)
            or any(package.startswith(f"{name}.") for package in self.config.production_packages)
        ):
            return False
        return path.is_file() or next(path.rglob("*.py"), None) is not None

    def _skipped(self, path: Path) -> bool:
        """
        Whether the walk skips ``path``.

        The built-in patterns never hide production code: a production package
        named ``build`` is still scanned. A configured pattern that would hide
        it is recorded as a problem, because the scan would silently find no
        sites there.
        """
        if is_excluded(path.name, self.config.exclude):
            if self._holds_production(path):
                self.hidden.append(self._relative(path).as_posix())
            return True
        return is_excluded(path.name, DEFAULT_EXCLUDE) and not self._holds_production(path)

    def _walk(self, base: Path) -> Iterator[Path]:
        """Every ``.py`` file under ``base`` the walk does not skip."""
        for directory, subdirectories, files in os.walk(base):
            here = Path(directory)
            subdirectories[:] = [name for name in sorted(subdirectories) if not self._skipped(here / name)]
            yield from (
                here / name for name in sorted(files) if name.endswith(".py") and not self._skipped(here / name)
            )

    def _add(self, path: Path) -> None:
        relative = self._relative(path)
        name = module_name(self.config, path)
        if name is None or name in self.modules or is_test_path(relative):
            return
        self.modules[name] = Module(
            name=name, path=path, relative=relative.as_posix(), package=path.name == "__init__.py"
        )

    @cached_property
    def production(self) -> list[Module]:
        return [module for name, module in self.modules.items() if self.config.is_production(name)]

    def tree(self, module: Module) -> ast.Module:
        """The parsed module; a file this Python cannot parse is recorded and read as empty."""
        if module.path not in self._trees:
            try:
                self._trees[module.path] = parse_module(module)
            except SyntaxError as error:
                version = f"{sys.version_info.major}.{sys.version_info.minor}"
                self.unparsable.append(
                    f"{module.relative}:{error.lineno} cannot be parsed by Python {version} ({error.msg}), so its "
                    f"handoff sites are unknown: run the check with the project's Python or newer"
                )
                self._trees[module.path] = ast.Module(body=[], type_ignores=[])
        return self._trees[module.path]

    def imports(self, module: Module) -> dict[str, str]:
        if module.name not in self._imports:
            self._imports[module.name] = top_level_imports(self.tree(module), module)
        return self._imports[module.name]

    def canonical(self, qualified: str) -> str:
        """Follow re-exports to the module that defines a name: ``shop.OrderService`` -> ``shop.orders.OrderService``."""
        for _ in range(32):
            parts = qualified.split(".")
            for cut in range(len(parts) - 1, 0, -1):
                module = self.modules.get(".".join(parts[:cut]))
                if module is not None:
                    bound = self.imports(module).get(parts[cut])
                    if bound is None:
                        return qualified
                    qualified = ".".join([bound, *parts[cut + 1 :]])
                    break
            else:
                return qualified
        return qualified

    @cached_property
    def reachable(self) -> list[Module]:
        """Production modules and every project module they import, transitively."""
        seen: dict[str, Module] = {}
        frontier = list(self.production)
        while frontier:
            module = frontier.pop()
            if module.name in seen:
                continue
            seen[module.name] = module
            for imported in imported_names(self.tree(module), module):
                for candidate in (imported, imported.rsplit(".", 1)[0]):
                    if candidate in self.modules and candidate not in seen:
                        frontier.append(self.modules[candidate])
        return list(seen.values())

    @cached_property
    def frameworks(self) -> set[str]:
        """Every dotted name imported anywhere in the code production reaches."""
        return {imported for module in self.reachable for imported in imported_names(self.tree(module), module)}


def parse_module(module: Module) -> ast.Module:
    return ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))


def terminal_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    if isinstance(expression, ast.Call):
        return terminal_name(expression.func)
    return None


def relative_source(node: ast.ImportFrom, module: Module) -> str:
    source = node.module or ""
    if not node.level:
        return source
    package = module.name if module.package else module.name.rpartition(".")[0]
    parts = package.split(".") if package else []
    anchor = parts[: max(len(parts) - node.level + 1, 0)]
    return ".".join([*anchor, source] if source else anchor)


def top_level_imports(tree: ast.Module, module: Module) -> dict[str, str]:
    """Local name -> the qualified name it refers to, from the module's top-level imports."""
    imported: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            source = relative_source(node, module)
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{source}.{alias.name}" if source else alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imported[alias.asname] = alias.name
                else:
                    imported[alias.name.split(".", 1)[0]] = alias.name.split(".", 1)[0]
    return imported


def imported_names(tree: ast.Module, module: Module) -> Iterator[str]:
    """Every dotted name the module imports, at any depth, relative imports resolved."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            source = relative_source(node, module)
            for alias in node.names:
                yield f"{source}.{alias.name}" if source else alias.name


def defined_names(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)}


def dotted_parts(expression: ast.expr) -> tuple[str, list[str]] | None:
    """``a.b.c`` -> ("a", ["b", "c"]); anything else -> None."""
    parts: list[str] = []
    while isinstance(expression, ast.Attribute):
        parts.append(expression.attr)
        expression = expression.value
    if not isinstance(expression, ast.Name):
        return None
    return expression.id, list(reversed(parts))


def qualified_name(expression: ast.expr, module: str, imported: Mapping[str, str], defined: set[str]) -> str | None:
    dotted = dotted_parts(expression)
    if dotted is None:
        return None
    base, rest = dotted
    if base in imported:
        head = imported[base]
    elif base in defined:
        head = f"{module}.{base}"
    else:
        return None
    return ".".join([head, *rest])
