"""
Handoff sites in production code, each attributed to the outermost function that makes it.

See :mod:`.sites` for what a site is. This module finds them: it indexes the
project's registered tasks, then visits every production module and records
each handoff reference against the function that contains it.
"""

import ast
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

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


class _Registrations:
    """Which decorators and registration calls in one module make a function a task of a kind."""

    def __init__(self, module: Module, project: Project) -> None:
        self.module = module
        self.project = project
        self.imported = project.imports(module)
        self.defined = defined_names(project.tree(module))

    def _decorator(self, expression: ast.expr, kind: SiteKind) -> bool:
        if terminal_name(expression) not in kind.task_decorators:
            return False
        if not kind.task_decorator_modules:
            return True
        target = expression.func if isinstance(expression, ast.Call) else expression
        qualified = qualified_name(target, self.module.name, self.imported, self.defined)
        qualified = None if qualified is None else self.project.canonical(qualified)
        return qualified is not None and any(
            qualified == module or qualified.startswith(f"{module}.") for module in kind.task_decorator_modules
        )

    def decorated(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: SiteKind) -> bool:
        return any(self._decorator(decorator, kind) for decorator in node.decorator_list)

    def registered(self, value: ast.expr, kind: SiteKind) -> bool:
        """``shared_task(fn)``, ``app.task(fn)`` or ``app.task(name=...)(fn)``: a task built by assignment."""
        if not isinstance(value, ast.Call) or not value.args:
            return False
        return self._decorator(value.func, kind)


def _task_index(project: Project, kinds: tuple[SiteKind, ...]) -> dict[str, set[str]]:
    """Kind name -> qualified names of the project functions its decorators or registrations make tasks."""
    index: dict[str, set[str]] = defaultdict(set)
    for module in project.reachable:
        registrations = _Registrations(module, project)
        for node in project.tree(module).body:
            for kind in kinds:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and registrations.decorated(node, kind):
                    index[kind.name].add(f"{module.name}.{node.name}")
                elif isinstance(node, ast.ClassDef):
                    for member in node.body:
                        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and registrations.decorated(
                            member, kind
                        ):
                            index[kind.name].add(f"{module.name}.{node.name}.{member.name}")
                elif (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and registrations.registered(node.value, kind)
                ):
                    index[kind.name].add(f"{module.name}.{node.targets[0].id}")
    return index


def _is_self(expression: ast.expr) -> bool:
    return isinstance(expression, ast.Name) and expression.id == "self"


#: Django's transaction block, however imported: ``transaction.atomic``, ``from django.db.transaction import atomic``.
ATOMIC = "django.db.transaction.atomic"


#: Kinds whose site is itself a deferral: ``on_commit``, and the project's forwarding helpers that call it.
AFTER_COMMIT_KINDS = frozenset({"django", "bridge"})


def _waits_for_the_commit(kind: str, node: ast.expr) -> bool:
    """Whether this handoff reference is a deferral: ``on_commit``, a forwarding helper, or a ``*_on_commit`` method."""
    return kind in AFTER_COMMIT_KINDS or (terminal_name(node) or "").endswith("_on_commit")


class _Scope:
    def __init__(self, name: str, function: bool) -> None:
        self.name = name
        self.function = function
        #: Local names bound to a handoff reference, and the kind of that reference.
        self.handoffs: dict[str, str] = {}
        #: Local names bound to a framework object that takes any callable (``queue = django_rq.get_queue()``).
        self.clients: dict[str, str] = {}
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
        seed: "_Sites | None" = None,
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
        #: ``self.<attribute>`` names bound to a handoff reference, or to a framework object, anywhere in the module.
        self.self_handoffs: dict[str, str] = {}
        self.self_clients: dict[str, str] = {}
        if seed is not None:
            top.handoffs.update(seed.scopes[0].handoffs)
            top.clients.update(seed.scopes[0].clients)
            self.self_handoffs.update(seed.self_handoffs)
            self.self_clients.update(seed.self_clients)
        self.scopes: list[_Scope] = [top]
        #: Depth of ``atomic`` blocks around the node being visited, in the current function.
        self.atomic_depth = 0
        #: Kinds whose queue can commit with the caller's data, so a handoff of that kind in a block is safe.
        self.joining = {kind.name for kind in kinds if kind.can_join_transaction}
        self.found: dict[str, list[Site]] = defaultdict(list)

    # Resolution

    def _local(self, name: str) -> tuple[str | None, str | None]:
        """(handoff kind, qualified name) a local name is bound to, innermost scope first."""
        for scope in reversed(self.scopes):
            if name in scope.handoffs:
                return scope.handoffs[name], None
            if name in scope.clients:
                return None, None
            if name in scope.names:
                return None, scope.names[name]
        return None, None

    def _client(self, expression: ast.expr) -> str | None:
        """The kind whose framework object an expression is: the framework, an object it returned, or a name for one."""
        if isinstance(expression, ast.Call):
            return self._client(expression.func)
        if isinstance(expression, ast.Name):
            for scope in reversed(self.scopes):
                if expression.id in scope.clients:
                    return scope.clients[expression.id]
                if expression.id in scope.handoffs or expression.id in scope.names:
                    break
        if (
            isinstance(expression, ast.Attribute)
            and _is_self(expression.value)
            and expression.attr in self.self_clients
        ):
            return self.self_clients[expression.attr]
        qualified = self._resolve(expression)
        if qualified is None:
            return None
        return next((kind.name for kind in self.kinds if kind.client_methods and kind.enabled_by(qualified)), None)

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
            return next(
                (
                    kind.name
                    for kind in self.kinds
                    if last in kind.calls or last in kind.client_methods and kind.enabled_by(qualified)
                ),
                None,
            )
        if not isinstance(expression, ast.Attribute):
            return None
        if expression.attr in self.config.bridge_methods or self._resolve(expression) in self.bridges:
            return "bridge"
        if _is_self(expression.value) and expression.attr in self.self_handoffs:
            return self.self_handoffs[expression.attr]
        for kind in self.kinds:
            if expression.attr in kind.calls:
                return kind.name
            if expression.attr in kind.task_methods and self._task(expression.value) in self.tasks.get(
                kind.name, set()
            ):
                return kind.name
            if expression.attr in kind.client_methods and self._client(expression.value) == kind.name:
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
        self.found[self._owner()].append(
            Site(
                kind=kind, path=self.module.relative, line=node.lineno, in_transaction=self._in_transaction(kind, node)
            )
        )

    def _in_transaction(self, kind: str, node: ast.expr) -> bool:
        """
        Whether the handoff can run before the block commits, or outlive its rollback.

        This is the whole judgement behind ``due-work-harness in-transaction``: the reference sits inside an
        ``atomic`` block, and it is not a deferral (``on_commit``) or a queue that commits with the block.
        """
        return self.atomic_depth > 0 and kind not in self.joining and not _waits_for_the_commit(kind, node)

    def _is_atomic(self, expression: ast.expr) -> bool:
        """``transaction.atomic``, ``atomic(using=...)``: the transaction block, as a decorator or a context."""
        return self._resolve(expression.func if isinstance(expression, ast.Call) else expression) == ATOMIC

    @contextmanager
    def _outside_the_block(self, *, atomic: bool = False) -> Iterator[None]:
        """
        Visit code that is written here but does not run here: a callback, or what is handed to ``on_commit``.

        A function decorated with ``atomic`` is the exception that runs inside its own transaction (``atomic=True``).
        """
        enclosing, self.atomic_depth = self.atomic_depth, int(atomic)
        try:
            yield
        finally:
            self.atomic_depth = enclosing

    def _with(self, node: ast.With | ast.AsyncWith) -> None:
        entered = sum(self._is_atomic(item.context_expr) for item in node.items)
        for item in node.items:
            self.visit(item)  # the context expressions run before the block is entered
        self.atomic_depth += entered
        for statement in node.body:
            self.visit(statement)
        self.atomic_depth -= entered

    visit_With = visit_AsyncWith = _with

    def visit_Lambda(self, node: ast.Lambda) -> None:
        with self._outside_the_block():
            self.generic_visit(node)

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
        # A decorated function runs entirely inside its transaction; a function defined inside a block does not.
        with self._outside_the_block(atomic=any(self._is_atomic(decorator) for decorator in node.decorator_list)):
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
        """
        Record ``target = value`` as an alias; True when the value needs no visit of its own.

        A handoff reference bound to a name is not itself a site (each use is); a
        framework object bound to a name (``queue = django_rq.get_queue()``) makes
        that name's handoff methods sites; a resolvable name is followed.
        """
        if value is None:
            return False
        client = self._client(value) if isinstance(value, ast.Call | ast.Name | ast.Attribute) else None
        if client is not None:
            if isinstance(target, ast.Name):
                self.scopes[-1].clients[target.id] = client
            elif isinstance(target, ast.Attribute) and _is_self(target.value):
                self.self_clients[target.attr] = client
            return False
        if not isinstance(value, ast.Name | ast.Attribute):
            return False
        kind = self.kind_of(value)
        if kind is not None:
            if isinstance(target, ast.Name):
                self.scopes[-1].handoffs[target.id] = kind
                return True
            if isinstance(target, ast.Attribute) and _is_self(target.value):
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
                break
        if self.kind_of(node.func) in AFTER_COMMIT_KINDS:
            # ``on_commit(partial(task.delay, pk))``: what it is given runs after the block, not in it.
            self.visit(node.func)
            with self._outside_the_block():
                for argument in (*node.args, *node.keywords):
                    self.visit(argument)
            return
        self.generic_visit(node)


def sites_by_function(project: Project, kinds: tuple[SiteKind, ...]) -> dict[str, list[Site]]:
    tasks = _task_index(project, kinds)
    sites: dict[str, list[Site]] = defaultdict(list)
    for module in project.production:
        # Two passes: the first learns every module-level and ``self.`` alias, so a use
        # that precedes its binding in the file (a method above ``__init__``) still counts.
        first = _Sites(module, project, kinds, tasks)
        first.visit(project.tree(module))
        visitor = _Sites(module, project, kinds, tasks, seed=first)
        visitor.visit(project.tree(module))
        for callable_name, found in visitor.found.items():
            sites[callable_name].extend(found)
    return dict(sites)


def production_sites(config: CoverageConfig) -> dict[str, list[Site]]:
    """Every handoff site in production, by the qualified name of the function that makes it."""
    project = Project(config)
    return sites_by_function(project, kinds_for(project.frameworks, config.kinds))


def sites_in_transaction(config: CoverageConfig) -> dict[str, list[Site]]:
    """
    The handoffs production makes inside a ``transaction.atomic()`` block, by function.

    A message published there can be consumed before the block commits (the worker
    finds no row) or outlive a rollback (the worker finds a row that never was).
    ``transaction.on_commit`` (and what is passed to it, or to a configured
    bridge) and Celery's ``*_on_commit`` variants wait for the commit and are not
    reported; nor are queues that can be a table in the caller's own database
    (Procrastinate's Django connector, django-tasks' database backend), where a
    job deferred in the block commits with the data. The scan is lexical: a handoff in a function
    that an atomic block calls, or in an ``ATOMIC_REQUESTS`` request, is not seen,
    and a callback defined inside the block is taken to run after it.
    """
    inside = {
        qualified: [site for site in sites if site.in_transaction]
        for qualified, sites in production_sites(config).items()
    }
    return {qualified: sites for qualified, sites in inside.items() if sites}
