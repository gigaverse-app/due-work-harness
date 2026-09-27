"""
Handoff sites in production code, each attributed to the outermost function that makes it.

See :mod:`.sites` for what a site is. This module finds them: it indexes the
project's registered tasks, then visits every production module and records
each handoff reference against the function that contains it.
"""

import ast
from collections import defaultdict
from collections.abc import Mapping

from due_work_harness.coverage.config import CoverageConfig
from due_work_harness.coverage.project import (
    Module,
    Project,
    defined_names,
    dotted_parts,
    qualified_name,
    relative_source,
    terminal_name,
)
from due_work_harness.coverage.report import MODULE_LEVEL, Site
from due_work_harness.coverage.sites import TASK_CONFIGURATORS, SiteKind, kinds_for


def _task_decorated(decorators: list[ast.expr], kind: SiteKind) -> bool:
    return any(terminal_name(decorator) in kind.task_decorators for decorator in decorators)


def _task_registration(value: ast.expr, kind: SiteKind) -> bool:
    """``shared_task(fn)``, ``app.task(fn)`` or ``app.task(name=...)(fn)``: a task built by assignment."""
    if not isinstance(value, ast.Call):
        return False
    callee = value.func
    if isinstance(callee, ast.Call):
        return terminal_name(callee.func) in kind.task_decorators and bool(value.args)
    return terminal_name(callee) in kind.task_decorators and bool(value.args)


def _task_index(project: Project, kinds: tuple[SiteKind, ...]) -> dict[str, set[str]]:
    """Kind name -> qualified names of the project functions its decorators or registrations make tasks."""
    index: dict[str, set[str]] = defaultdict(set)
    for module in project.reachable:
        for node in project.tree(module).body:
            for kind in kinds:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _task_decorated(
                    node.decorator_list, kind
                ):
                    index[kind.name].add(f"{module.name}.{node.name}")
                elif isinstance(node, ast.ClassDef):
                    for member in node.body:
                        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and _task_decorated(
                            member.decorator_list, kind
                        ):
                            index[kind.name].add(f"{module.name}.{node.name}.{member.name}")
                elif (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and _task_registration(node.value, kind)
                ):
                    index[kind.name].add(f"{module.name}.{node.targets[0].id}")
    return index


class _Scope:
    def __init__(self, name: str, function: bool) -> None:
        self.name = name
        self.function = function
        #: Local names bound to a handoff reference, and the kind of that reference.
        self.handoffs: dict[str, str] = {}
        #: Local names bound to a resolvable name (``send = tasks.send_receipt``).
        self.names: dict[str, str] = {}


class _Sites(ast.NodeVisitor):
    """Every handoff reference in one production module, attributed to its outermost function."""

    def __init__(
        self,
        module: Module,
        project: Project,
        kinds: tuple[SiteKind, ...],
        tasks: Mapping[str, set[str]],
        *,
        module_handoffs: Mapping[str, str],
        self_handoffs: Mapping[str, str],
    ) -> None:
        self.module = module
        self.project = project
        self.config = project.config
        self.kinds = kinds
        self.tasks = tasks
        tree = project.tree(module)
        self.imported = project.imports(module)
        self.defined = defined_names(tree)
        self.bridges = {project.canonical(name) for name in self.config.bridges}
        top = _Scope(MODULE_LEVEL, function=False)
        top.handoffs.update(module_handoffs)
        self.scopes: list[_Scope] = [top]
        #: ``self.<attribute>`` names bound to a handoff reference anywhere in the module.
        self.self_handoffs: dict[str, str] = dict(self_handoffs)
        self.found: dict[str, list[Site]] = defaultdict(list)

    # Resolution

    def _local(self, name: str) -> tuple[str | None, str | None]:
        """(handoff kind, qualified name) a local name is bound to, innermost scope first."""
        for scope in reversed(self.scopes):
            if name in scope.handoffs:
                return scope.handoffs[name], None
            if name in scope.names:
                return None, scope.names[name]
        return None, None

    def _resolve(self, expression: ast.expr) -> str | None:
        dotted = dotted_parts(expression)
        if dotted is None:
            return None
        base, rest = dotted
        _, local = self._local(base)
        if local is not None:
            qualified = ".".join([local, *rest])
        else:
            qualified = qualified_name(expression, self.module.name, self.imported, self.defined)
        return None if qualified is None else self.project.canonical(qualified)

    def _task(self, expression: ast.expr) -> str | None:
        while (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr in TASK_CONFIGURATORS
        ):
            expression = expression.func.value
        return self._resolve(expression)

    def kind_of(self, expression: ast.expr) -> str | None:
        """The kind of handoff a name or attribute refers to, or None."""
        if isinstance(expression, ast.Name):
            local, _ = self._local(expression.id)
            if local is not None:
                return local
            qualified = self._resolve(expression)
            if qualified is None:
                return None
            if qualified in self.bridges:
                return "bridge"
            last = qualified.rsplit(".", 1)[-1]
            return next((kind.name for kind in self.kinds if last in kind.calls), None)
        if not isinstance(expression, ast.Attribute):
            return None
        if expression.attr in self.config.bridge_methods or self._resolve(expression) in self.bridges:
            return "bridge"
        if (
            isinstance(expression.value, ast.Name)
            and expression.value.id == "self"
            and expression.attr in self.self_handoffs
        ):
            return self.self_handoffs[expression.attr]
        for kind in self.kinds:
            if expression.attr in kind.calls:
                return kind.name
            if expression.attr in kind.task_methods and self._task(expression.value) in self.tasks.get(
                kind.name, set()
            ):
                return kind.name
        return None

    # Attribution

    def _owner(self) -> str:
        """The outermost function (with its classes) containing the current node, or ``<module>``."""
        path: list[str] = []
        for scope in self.scopes[1:]:
            path.append(scope.name)
            if scope.function:
                return f"{self.module.name}.{'.'.join(path)}"
        return f"{self.module.name}.{MODULE_LEVEL}"

    def _record(self, kind: str, node: ast.expr) -> None:
        self.found[self._owner()].append(Site(kind=kind, path=self.module.relative, line=node.lineno))

    # Scopes and bindings

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        scope = _Scope(node.name, function=True)
        arguments = node.args
        positional = [*arguments.posonlyargs, *arguments.args]
        defaults: list[tuple[ast.arg, ast.expr | None]] = [
            *zip(positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True),
            *zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True),
        ]
        for argument, default in defaults:
            if default is None:
                continue
            # ``def place(self, hook=transaction.on_commit)`` binds an alias; each use of ``hook`` is a site.
            kind = self.kind_of(default)
            if kind is None:
                self.visit(default)
            else:
                scope.handoffs[argument.arg] = kind
        self.scopes.append(scope)
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(expression)
        self.scopes.append(_Scope(node.name, function=False))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _bind(self, target: ast.expr, value: ast.expr | None) -> bool:
        """Record ``target = value`` as an alias when value is a handoff or a resolvable name; True if handled."""
        if value is None or not isinstance(value, ast.Name | ast.Attribute):
            return False
        kind = self.kind_of(value)
        if kind is not None:
            if isinstance(target, ast.Name):
                self.scopes[-1].handoffs[target.id] = kind
                return True
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                self.self_handoffs[target.attr] = kind
                return True
            return False
        resolved = self._resolve(value)
        if resolved is not None and isinstance(target, ast.Name):
            self.scopes[-1].names[target.id] = resolved
        return False

    def visit_Import(self, node: ast.Import) -> None:
        if len(self.scopes) > 1:  # module-level imports are the module's own table
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                self.scopes[-1].names[local] = alias.name if alias.asname else local

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if len(self.scopes) > 1:
            source = relative_source(node, self.module)
            for alias in node.names:
                self.scopes[-1].names[alias.asname or alias.name] = f"{source}.{alias.name}" if source else alias.name

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and self._bind(node.targets[0], node.value):
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._bind(node.target, node.value):
            return
        self.generic_visit(node)

    # References

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            kind = self.kind_of(node)
            if kind is not None:
                self._record(kind, node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            kind = self.kind_of(node)
            if kind is not None:
                self._record(kind, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = terminal_name(node.func)
        for kind in self.kinds:
            if (
                name in kind.task_argument_calls
                and node.args
                and self._task(node.args[0]) in self.tasks.get(kind.name, set())
            ):
                self._record(kind.name, node)
        self.generic_visit(node)


def sites_by_function(project: Project, kinds: tuple[SiteKind, ...]) -> dict[str, list[Site]]:
    tasks = _task_index(project, kinds)
    sites: dict[str, list[Site]] = defaultdict(list)
    for module in project.production:
        # Two passes: the first learns every module-level and ``self.`` alias, so a use
        # that precedes its binding in the file (a method above ``__init__``) still counts.
        first = _Sites(module, project, kinds, tasks, module_handoffs={}, self_handoffs={})
        first.visit(project.tree(module))
        visitor = _Sites(
            module,
            project,
            kinds,
            tasks,
            module_handoffs=first.scopes[0].handoffs,
            self_handoffs=first.self_handoffs,
        )
        visitor.visit(project.tree(module))
        for callable_name, found in visitor.found.items():
            sites[callable_name].extend(found)
    return dict(sites)


def production_sites(config: CoverageConfig) -> dict[str, list[Site]]:
    """Every handoff site in production, by the qualified name of the function that makes it."""
    project = Project(config)
    return sites_by_function(project, kinds_for(project.frameworks, config.kinds))
