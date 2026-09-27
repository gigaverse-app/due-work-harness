"""
Declarations in test modules: the contract and exemption suites that account for sites.

A declaration counts only when pytest will collect and run it as written, and
only when it is the harness's own; see :mod:`.scan` for the rules.
"""

import ast
import os
from collections.abc import Iterator
from pathlib import Path

from due_work_harness.binding import MINIMUM_REASON_LENGTH
from due_work_harness.coverage.config import DEFAULT_EXCLUDE, CoverageConfig
from due_work_harness.coverage.project import (
    Module,
    Project,
    defined_names,
    is_excluded,
    module_name,
    qualified_name,
    terminal_name,
    top_level_imports,
)
from due_work_harness.coverage.report import Disposition

HARNESS = "due_work_harness"
SUITES = {"due_work_contract_suite": "DueWorkContract", "scheduled_selection_suite": "ScheduledSelection"}
EXEMPTION_SUITE = "exempt_due_work_suite"
_MARKERS = (*SUITES, EXEMPTION_SUITE)
#: pytest marks that stop a declared suite from running or from failing.
_SKIPPING_MARKS = frozenset({"skip", "skipif", "xfail"})


def declaration_modules(config: CoverageConfig) -> list[Module]:
    found: list[Module] = []
    seen: set[Path] = set()
    for test_path in config.test_paths:
        base = (config.root / test_path).resolve()
        if not base.exists():
            continue
        paths = [base] if base.is_file() else _declaration_candidates(config, base)
        for path in paths:
            if path in seen or not path.name.startswith("test_") and not path.stem.endswith("_test"):
                continue
            seen.add(path)
            if any(marker in path.read_text(encoding="utf-8") for marker in _MARKERS):
                name = module_name(config, path) or path.stem
                found.append(
                    Module(name=name, path=path, relative=path.relative_to(config.root).as_posix(), package=False)
                )
    return found


def _declaration_candidates(config: CoverageConfig, base: Path) -> Iterator[Path]:
    for directory, subdirectories, files in os.walk(base):
        subdirectories[:] = sorted(
            name for name in subdirectories if not is_excluded(name, (*DEFAULT_EXCLUDE, *config.exclude))
        )
        yield from (Path(directory) / name for name in sorted(files) if name.endswith(".py"))


def _binds(statement: ast.stmt, name: str) -> bool:
    """Whether a module- or class-level statement (re)binds ``name``."""
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return statement.name == name
    if isinstance(statement, ast.Assign | ast.Delete):
        targets = statement.targets
    elif isinstance(statement, ast.AnnAssign):
        targets = [statement.target]
    else:
        return False
    return any(isinstance(target, ast.Name) and target.id == name for target in targets)


class Declarations:
    """The dispositions one test module declares, and every way a declaration would not run as counted."""

    def __init__(self, module: Module, project: Project) -> None:
        self.module = module
        self.project = project
        self.tree = project.tree(module)
        self.imported = top_level_imports(self.tree, module)
        self.defined = defined_names(self.tree)
        self.assigned = {
            target.id: node.value
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.found: dict[str, Disposition] = {}
        self.defects: list[str] = []

    def _qualified(self, expression: ast.expr) -> str | None:
        qualified = qualified_name(expression, self.module.name, self.imported, self.defined)
        return None if qualified is None else self.project.canonical(qualified)

    def _harness(self, expression: ast.expr) -> str | None:
        """The due_work_harness name an expression refers to, or None when it is not the harness's."""
        qualified = qualified_name(expression, self.module.name, self.imported, self.defined)
        if qualified is None or qualified.split(".", 1)[0] != HARNESS:
            return None
        return qualified.rsplit(".", 1)[-1]

    def _constructed(self, name: str) -> str | None:
        """The harness constructor a module-level ``NAME = Constructor(...)`` uses, or None."""
        value = self.assigned.get(name)
        return self._harness(value.func) if isinstance(value, ast.Call) else None

    def _add(self, qualified: str, disposition: Disposition, owner: str) -> None:
        if qualified in self.found:
            self.defects.append(f"{owner}: {qualified} is accounted for more than once in this module")
        else:
            self.found[qualified] = disposition

    def _source(self, call: ast.expr) -> tuple[str | None, int, str | None]:
        """``DueWorkSource(callable, sites=N)`` -> (qualified callable, sites, defect)."""
        if not isinstance(call, ast.Call) or terminal_name(call.func) != "DueWorkSource":
            return None, 0, "entries must be typed DueWorkSource(callable) values, not paths or other metadata"
        if self._harness(call.func) != "DueWorkSource":
            return None, 0, "DueWorkSource must be due_work_harness's; import it from due_work_harness"
        if len(call.args) != 1:
            return None, 0, "DueWorkSource must receive exactly one production callable"
        qualified = self._qualified(call.args[0])
        if qualified is None:
            return (
                None,
                0,
                "DueWorkSource must receive an imported production callable, such as OrderService.place_order",
            )
        sites = 1
        for keyword in call.keywords:
            if keyword.arg == "sites":
                if not isinstance(keyword.value, ast.Constant) or type(keyword.value.value) is not int:
                    return None, 0, "DueWorkSource.sites must be a literal integer, so the scan can read it"
                sites = keyword.value.value
        if sites < 1:
            return None, 0, "DueWorkSource.sites must be at least one"
        return qualified, sites, None

    def _proof_defect(self, proof: ast.expr) -> str | None:
        """Why ``prove=`` is test-authored, or None when it is a harness probe or production callable."""
        target = proof.func if isinstance(proof, ast.Call) else proof
        if isinstance(target, ast.Name) and isinstance(bound := self.assigned.get(target.id), ast.Call):
            target = bound.func  # PROOF = LossIsAbsorbedElsewhere(...)
        qualified = self._qualified(target)
        if qualified is not None and (
            qualified.split(".", 1)[0] == HARNESS or self.project.config.is_production(qualified)
        ):
            return None
        return (
            "prove= must be a harness probe, such as LossIsAbsorbedElsewhere(...), or a production callable: "
            "a proof written in the test can make any loss look absorbed"
        )

    def _skipping(self, expression: ast.expr) -> list[str]:
        return sorted(
            {
                node.attr
                for node in ast.walk(expression)
                if isinstance(node, ast.Attribute)
                and node.attr in _SKIPPING_MARKS
                and terminal_name(node.value) == "mark"
            }
        )

    def _would_not_run(self, node: ast.ClassDef, index: int | None) -> str | None:
        """Why pytest would not collect and run this class as written, or None."""
        if index is None:
            return "is not a module-level class, so pytest never collects it"
        if not node.name.startswith("Test"):
            return "is not a collected Test* class"
        rebound = next((later for later in self.tree.body[index + 1 :] if _binds(later, node.name)), None)
        if rebound is not None:
            return f"is rebound on line {rebound.lineno}, so pytest collects the later binding instead"
        if any(_binds(statement, "__test__") for statement in node.body):
            return "sets __test__, so pytest may not collect it"
        marked = [*node.decorator_list] + [
            statement.value
            for statement in (*node.body, *self.tree.body)
            if isinstance(statement, ast.Assign) and _binds(statement, "pytestmark")
        ]
        marks = sorted({mark for expression in marked for mark in self._skipping(expression)})
        if marks:
            return f"carries pytest.mark.{marks[0]}, so its cases would not run or could not fail"
        return None

    def collect(self) -> tuple[dict[str, Disposition], list[str]]:
        positions = {id(node): index for index, node in enumerate(self.tree.body)}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and terminal_name(decorator.func) in _MARKERS:
                        self._declaration(node, decorator, positions.get(id(node)))
        return self.found, self.defects

    def _declaration(self, node: ast.ClassDef, decorator: ast.Call, index: int | None) -> None:
        suite = terminal_name(decorator.func)
        assert suite is not None
        owner = where = f"{self.module.relative}::{node.name}"
        if self._harness(decorator.func) != suite:
            self.defects.append(f"{owner}: {suite} is not due_work_harness's; import it from due_work_harness")
            return
        reason = self._would_not_run(node, index)
        if reason is not None:
            self.defects.append(f"{owner}: uses {suite} but {reason}")
            return
        if suite == EXEMPTION_SUITE:
            self._exemption(decorator, owner, where)
            return
        expected = SUITES[suite]
        covers = [keyword for keyword in decorator.keywords if keyword.arg == "covers"]
        if not covers:
            return
        if not decorator.args or not isinstance(decorator.args[0], ast.Name):
            self.defects.append(f"{owner}: {suite} must receive a named, module-level {expected} first")
            return
        if self._constructed(decorator.args[0].id) != expected:
            self.defects.append(
                f"{owner}: {suite} references {decorator.args[0].id}, which this module does not build as "
                f"due_work_harness's {expected}"
            )
            return
        if len(covers) != 1 or not isinstance(covers[0].value, ast.Tuple | ast.List):
            self.defects.append(f"{owner}: covers= must be one inline tuple of DueWorkSource values")
            return
        for entry in covers[0].value.elts:
            qualified, sites, defect = self._source(entry)
            if defect:
                self.defects.append(f"{owner}: covers {defect}")
            else:
                assert qualified is not None
                self._add(qualified, Disposition(how="covered", sites=sites, where=where), owner)

    def _exemption(self, decorator: ast.Call, owner: str, where: str) -> None:
        if len(decorator.args) != 1:
            self.defects.append(f"{owner}: {EXEMPTION_SUITE} takes one DueWorkSource")
            return
        qualified, sites, defect = self._source(decorator.args[0])
        keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
        reason = keywords.get("reason")
        if defect is None and "prove" not in keywords:
            defect = "an exemption must carry prove=, executable evidence that the loss is absorbed"
        if defect is None:
            defect = self._proof_defect(keywords["prove"])
        if defect is None and not (isinstance(reason, ast.Constant) and isinstance(reason.value, str)):
            defect = "reason= must be a literal string, so the scan can review it"
        if (
            defect is None
            and isinstance(reason, ast.Constant)
            and len(str(reason.value).strip()) < MINIMUM_REASON_LENGTH
        ):
            defect = "the exemption's reason is too thin to name what absorbs the loss"
        if defect:
            self.defects.append(f"{owner}: {defect}")
            return
        assert qualified is not None
        self._add(qualified, Disposition(how="exempt", sites=sites, where=where), owner)
