"""
Every handoff site in production has exactly one disposition.

The scan is static in both directions and imports nothing: it parses production
modules for handoff sites (see :mod:`.sites`), attributing each to the exact
function or method that contains it, and parses test modules for the
declarations that account for them. It then requires the two to match exactly:

* **covered** — a generated contract suite names the function::

      @due_work_contract_suite(MY_CONTRACT, covers=(DueWorkSource(OrderService.place_order),))
      class TestOrdersDueWork: ...

* **exempt** — a generated exemption suite states why losing the handoff is
  acceptable and proves it::

      @exempt_due_work_suite(
          DueWorkSource(cache.evict_after_commit),
          reason="the cache entry expires within a minute; the database stays authoritative",
          prove=LossIsAbsorbedElsewhere(strand=..., observe=..., absorb=...),
      )
      class TestCacheEvictionExemption: ...

* **baseline** — the site predates adoption and is listed in
  ``[tool.due-work-harness.baseline]``, which only shrinks.

A site with no disposition fails, and so does a disposition naming a function
with no site (stale), a function whose number of sites changed (drift), a
function with two dispositions, and a malformed declaration: ``covers`` must be
one inline tuple of ``DueWorkSource(<imported production callable>)``, the
contract must be a named module-level ``DueWorkContract`` (or
``ScheduledSelection``) built in the same module, and the class must be a
collected ``Test*`` class. Matching is by exact qualified name, so two methods
in one file can never hide behind each other, and test modules are never
imported, so the check runs anywhere, fast, with no framework or database.
"""

import ast
import fnmatch
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from due_work_harness.binding import MINIMUM_REASON_LENGTH
from due_work_harness.coverage.config import CoverageConfig
from due_work_harness.coverage.sites import CALL_WRAPPERS, TASK_CONFIGURATORS, SiteKind

SUITES = {"due_work_contract_suite": "DueWorkContract", "scheduled_selection_suite": "ScheduledSelection"}
EXEMPTION_SUITE = "exempt_due_work_suite"
_MARKERS = (*SUITES, EXEMPTION_SUITE)


@dataclass(frozen=True)
class Site:
    """One handoff call: its kind and where it is."""

    kind: str
    path: str
    line: int


@dataclass(frozen=True)
class Disposition:
    """How one production function's handoffs are accounted for."""

    how: str  # "covered", "exempt", "baseline", "bridge"
    sites: int
    where: str


@dataclass
class CoverageReport:
    """The scan's result: sites by function, their dispositions, and every problem found."""

    sites: dict[str, list[Site]] = field(default_factory=dict)
    dispositions: dict[str, Disposition] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def unaccounted(self) -> list[str]:
        return sorted(set(self.sites) - set(self.dispositions))

    def raise_for_problems(self) -> None:
        if self.problems:
            raise AssertionError("due-work coverage:\n  " + "\n  ".join(self.problems))


@dataclass(frozen=True)
class _Module:
    name: str
    path: Path
    relative: str


def _excluded(relative: Path, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for part in relative.parts for pattern in patterns)


def _python_files(config: CoverageConfig, start: Path) -> Iterator[Path]:
    for path in sorted(start.rglob("*.py")):
        if not _excluded(path.relative_to(config.root), config.exclude):
            yield path


def _module_name(config: CoverageConfig, path: Path) -> str | None:
    for root in config.source_roots:
        base = (config.root / root).resolve()
        if base in path.parents:
            parts = list(path.relative_to(base).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            return ".".join(parts)
    return None


def is_test_path(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith(("test_", "conftest")) or path.stem.endswith("_test")


def _is_production(config: CoverageConfig, name: str) -> bool:
    return any(name == package or name.startswith(f"{package}.") for package in config.production_packages)


def production_modules(config: CoverageConfig) -> list[_Module]:
    modules: dict[str, _Module] = {}
    for root in config.source_roots:
        base = (config.root / root).resolve()
        if not base.is_dir():
            continue
        for path in _python_files(config, base):
            name = _module_name(config, path)
            if name is None or name in modules or is_test_path(path) or not _is_production(config, name):
                continue
            modules[name] = _Module(name, path, path.relative_to(config.root).as_posix())
    return list(modules.values())


def declaration_modules(config: CoverageConfig) -> list[_Module]:
    found: list[_Module] = []
    seen: set[Path] = set()
    for test_path in config.test_paths:
        base = (config.root / test_path).resolve()
        if not base.exists():
            continue
        for path in [base] if base.is_file() else _python_files(config, base):
            if path in seen or not path.name.startswith("test_") and not path.stem.endswith("_test"):
                continue
            seen.add(path)
            if any(marker in path.read_text(encoding="utf-8") for marker in _MARKERS):
                found.append(
                    _Module(_module_name(config, path) or path.stem, path, path.relative_to(config.root).as_posix())
                )
    return found


def _parse(module: _Module) -> ast.Module:
    return ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))


# --- Name resolution -------------------------------------------------------------------------


def _terminal(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    if isinstance(expression, ast.Call):
        return _terminal(expression.func)
    return None


def _imports(tree: ast.Module, module: str) -> dict[str, str]:
    """Local name -> the qualified name it refers to, from the module's top-level imports."""
    package = module.rsplit(".", 1)[0] if "." in module else ""
    imported: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if node.level:
                anchor = package.split(".")[: max(len(package.split(".")) - node.level + 1, 0)]
                source = ".".join([*anchor, source] if source else anchor)
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{source}.{alias.name}" if source else alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imported[alias.asname] = alias.name
                else:
                    imported[alias.name.split(".", 1)[0]] = alias.name.split(".", 1)[0]
    return imported


def _defined(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)}


def _qualified(expression: ast.expr, module: str, imported: Mapping[str, str], defined: set[str]) -> str | None:
    parts: list[str] = []
    while isinstance(expression, ast.Attribute):
        parts.append(expression.attr)
        expression = expression.value
    if not isinstance(expression, ast.Name):
        return None
    if expression.id in imported:
        head = imported[expression.id]
    elif expression.id in defined:
        head = f"{module}.{expression.id}"
    else:
        return None
    return ".".join([head, *reversed(parts)])


# --- Production sites ------------------------------------------------------------------------


def _task_index(modules: list[tuple[_Module, ast.Module]], kinds: tuple[SiteKind, ...]) -> dict[str, set[str]]:
    """Kind name -> qualified names of production functions its decorators register as tasks."""
    index: dict[str, set[str]] = defaultdict(set)
    for module, tree in modules:
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            decorators = {_terminal(decorator) for decorator in node.decorator_list}
            for kind in kinds:
                if decorators & kind.task_decorators:
                    index[kind.name].add(f"{module.name}.{node.name}")
    return index


class _Sites(ast.NodeVisitor):
    def __init__(
        self,
        module: _Module,
        tree: ast.Module,
        config: CoverageConfig,
        tasks: Mapping[str, set[str]],
    ) -> None:
        self.module = module
        self.config = config
        self.tasks = tasks
        self.imported = _imports(tree, module.name)
        self.defined = _defined(tree)
        self.scopes: list[str] = []
        self.depth = 0
        self.found: dict[str, list[Site]] = defaultdict(list)

    def _scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        is_function = not isinstance(node, ast.ClassDef)
        self.depth += is_function
        self.generic_visit(node)
        self.depth -= is_function
        self.scopes.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scope

    def _resolve(self, expression: ast.expr) -> str | None:
        return _qualified(expression, self.module.name, self.imported, self.defined)

    def _task_receiver(self, expression: ast.expr) -> str | None:
        while (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr in TASK_CONFIGURATORS
        ):
            expression = expression.func.value
        return self._resolve(expression)

    def _kind_of(self, node: ast.Call) -> str | None:
        callee = node.func
        # sync_to_async(on_commit)(...) is on_commit.
        if isinstance(callee, ast.Call) and _terminal(callee.func) in CALL_WRAPPERS and callee.args:
            callee = callee.args[0]
        name = _terminal(callee)
        if name is None:
            return None
        if self._resolve(callee) in self.config.bridges or (
            isinstance(callee, ast.Attribute) and name in self.config.bridge_methods
        ):
            return "bridge"
        for kind in self.config.kinds:
            if name in kind.calls:
                return kind.name
            if name in kind.task_methods and isinstance(callee, ast.Attribute):
                if self._task_receiver(callee.value) in self.tasks.get(kind.name, set()):
                    return kind.name
            if name in kind.task_argument_calls and node.args:
                if self._resolve(node.args[0]) in self.tasks.get(kind.name, set()):
                    return kind.name
        return None

    def visit_Call(self, node: ast.Call) -> None:
        kind = self._kind_of(node)
        if kind is not None:
            scope = ".".join(self.scopes) if self.depth else "<module>"
            self.found[f"{self.module.name}.{scope}"].append(Site(kind, self.module.relative, node.lineno))
        self.generic_visit(node)


def production_sites(config: CoverageConfig) -> dict[str, list[Site]]:
    parsed = [(module, _parse(module)) for module in production_modules(config)]
    tasks = _task_index(parsed, config.kinds)
    sites: dict[str, list[Site]] = defaultdict(list)
    for module, tree in parsed:
        visitor = _Sites(module, tree, config, tasks)
        visitor.visit(tree)
        for callable_name, found in visitor.found.items():
            sites[callable_name].extend(found)
    return dict(sites)


# --- Declarations ----------------------------------------------------------------------------


def _constructed(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = DueWorkContract(...)``: name -> constructor."""
    built: dict[str, str] = {}
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
        target = node.target if isinstance(node, ast.AnnAssign) else target
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if isinstance(target, ast.Name) and isinstance(value, ast.Call):
            constructor = _terminal(value.func)
            if constructor is not None and constructor in SUITES.values():
                built[target.id] = constructor
    return built


def _source(
    call: ast.expr, module: _Module, imported: Mapping[str, str], defined: set[str]
) -> tuple[str | None, int, str | None]:
    """``DueWorkSource(callable, sites=N)`` -> (qualified callable, sites, defect)."""
    if not isinstance(call, ast.Call) or _terminal(call.func) != "DueWorkSource":
        return None, 0, "entries must be typed DueWorkSource(callable) values, not paths or other metadata"
    if len(call.args) != 1:
        return None, 0, "DueWorkSource must receive exactly one production callable"
    qualified = _qualified(call.args[0], module.name, imported, defined)
    if qualified is None:
        return None, 0, "DueWorkSource must receive an imported production callable, such as OrderService.place_order"
    sites = 1
    for keyword in call.keywords:
        if keyword.arg == "sites":
            if not isinstance(keyword.value, ast.Constant) or not isinstance(keyword.value.value, int):
                return None, 0, "DueWorkSource.sites must be a literal integer, so the scan can read it"
            sites = keyword.value.value
    if sites < 1:
        return None, 0, "DueWorkSource.sites must be at least one"
    return qualified, sites, None


def _declarations(module: _Module) -> tuple[dict[str, Disposition], list[str]]:
    tree = _parse(module)
    imported, defined, contracts = _imports(tree, module.name), _defined(tree), _constructed(tree)
    found: dict[str, Disposition] = {}
    defects: list[str] = []

    def add(qualified: str, disposition: Disposition, owner: str) -> None:
        if qualified in found:
            defects.append(f"{owner}: {qualified} is accounted for more than once in this module")
        else:
            found[qualified] = disposition

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for decorator in node.decorator_list:
            suite = _terminal(decorator.func) if isinstance(decorator, ast.Call) else None
            if suite is None or suite not in _MARKERS or not isinstance(decorator, ast.Call):
                continue
            owner = f"{module.relative}::{node.name}"
            if not node.name.startswith("Test"):
                defects.append(f"{owner}: uses {suite} but is not a collected Test* class")
                continue
            where = f"{module.relative}::{node.name}"
            if suite == EXEMPTION_SUITE:
                if len(decorator.args) != 1:
                    defects.append(f"{owner}: {suite} takes one DueWorkSource")
                    continue
                qualified, sites, defect = _source(decorator.args[0], module, imported, defined)
                keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
                reason = keywords.get("reason")
                if defect:
                    defects.append(f"{owner}: {defect}")
                elif "prove" not in keywords:
                    defects.append(
                        f"{owner}: an exemption must carry prove=, executable evidence that the loss is absorbed"
                    )
                elif not (isinstance(reason, ast.Constant) and isinstance(reason.value, str)):
                    defects.append(f"{owner}: reason= must be a literal string, so the scan can review it")
                elif len(reason.value.strip()) < MINIMUM_REASON_LENGTH:
                    defects.append(f"{owner}: the exemption's reason is too thin to name what absorbs the loss")
                else:
                    assert qualified is not None
                    add(qualified, Disposition("exempt", sites, where), owner)
                continue
            expected = SUITES[suite]
            covers = [keyword for keyword in decorator.keywords if keyword.arg == "covers"]
            if not covers:
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Name):
                defects.append(f"{owner}: {suite} must receive a named, module-level {expected} first")
                continue
            if contracts.get(decorator.args[0].id) != expected:
                defects.append(
                    f"{owner}: {suite} references {decorator.args[0].id}, which this module does not build as {expected}"
                )
                continue
            if len(covers) != 1 or not isinstance(covers[0].value, ast.Tuple | ast.List):
                defects.append(f"{owner}: covers= must be one inline tuple of DueWorkSource values")
                continue
            for entry in covers[0].value.elts:
                qualified, sites, defect = _source(entry, module, imported, defined)
                if defect:
                    defects.append(f"{owner}: covers {defect}")
                else:
                    assert qualified is not None
                    add(qualified, Disposition("covered", sites, where), owner)
    return found, defects


# --- The check -------------------------------------------------------------------------------


def scan(config: CoverageConfig) -> CoverageReport:
    """Find every site and every disposition, and every way they fail to match."""
    report = CoverageReport(sites=production_sites(config))
    declared: dict[str, list[Disposition]] = defaultdict(list)
    for module in declaration_modules(config):
        found, defects = _declarations(module)
        report.problems.extend(defects)
        for qualified, disposition in found.items():
            declared[qualified].append(disposition)
    for qualified, count in config.baseline.items():
        declared[qualified].append(Disposition("baseline", count, "pyproject.toml [tool.due-work-harness.baseline]"))
    for qualified, count in config.bridges.items():
        declared[qualified].append(Disposition("bridge", count, "pyproject.toml [tool.due-work-harness] bridges"))

    for qualified, dispositions in sorted(declared.items()):
        if len(dispositions) > 1:
            places = ", ".join(f"{d.how} in {d.where}" for d in dispositions)
            report.problems.append(f"{qualified} has more than one disposition: {places}")
        report.dispositions[qualified] = dispositions[0]

    for qualified in report.unaccounted:
        first = report.sites[qualified][0]
        kinds = ", ".join(sorted(Counter(site.kind for site in report.sites[qualified])))
        report.problems.append(
            f"{qualified} ({first.path}:{first.line}, {kinds}) hands work off with no disposition: cover it with "
            f"DueWorkSource on the contract suite that insures it, or exempt it with proof"
        )
    for qualified, disposition in sorted(report.dispositions.items()):
        actual = len(report.sites.get(qualified, []))
        if actual == 0:
            report.problems.append(
                f"{qualified} is {disposition.how} ({disposition.where}) but has no handoff site: remove the stale "
                f"disposition, or point it at the function that now hands the work off"
            )
        elif actual != disposition.sites:
            report.problems.append(
                f"{qualified} has {actual} handoff site(s) but its disposition ({disposition.how}, "
                f"{disposition.where}) accounts for {disposition.sites}: review it and update sites="
            )
    return report


def baseline_growth(current: Mapping[str, int], previous: Mapping[str, int]) -> list[str]:
    """Baseline entries added since ``previous``; the baseline only shrinks."""
    return sorted(set(current) - set(previous))


def unaccounted_baseline(report: CoverageReport) -> dict[str, int]:
    """A baseline that would account for every currently unaccounted site, for first adoption."""
    return {qualified: len(report.sites[qualified]) for qualified in report.unaccounted}
