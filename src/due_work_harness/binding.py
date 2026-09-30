"""
Contract adapters bind production semantics; they do not reproduce them.

The harness owns invariant assertions. A domain adapter has only three jobs:

* **arrange** domain examples and failure conditions;
* **invoke** the production selector or transition the invariant exercises;
* **observe** the result in the domain's vocabulary.

Only the middle category is load-bearing production semantics. If a test-side
callback reconstructs an ORM predicate or state transition, the generated test
proves that reconstruction rather than the runtime. The copy can remain green
after production changes, which is worse than an absent test because it reports
false confidence.

This module is the shared enforcement point. Profile modules apply
:func:`assert_test_binding_forwards` only to semantic callbacks, with the
operations that would mean the adapter authored that semantic. Fixture makers,
fault injection and observers are deliberately not checked: direct ORM access
is appropriate there because they arrange or measure a state rather than define
what production will select or write.

The check follows test-authored helper calls recursively, so moving a copied
query behind ``_copied_selection()`` does not evade it. It is intentionally a
focused authorship tripwire, not a general clone detector: arbitrary programs
cannot be proven equivalent syntactically. The positive architecture remains
simple and reviewable — extract one production callable, make runtime call it,
and make the adapter forward to it.
"""

import ast
import builtins
import contextlib
import dis
import functools
import inspect
import sys
import textwrap
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from pathlib import Path
from types import CodeType, FunctionType, MethodType, ModuleType
from typing import Any

import pytest

from due_work_harness.host import production_packages

SELECTION_AUTHORING_OPERATIONS = frozenset(
    {
        "Q",
        "alias",
        "annotate",
        "exclude",
        "filter",
        "order_by",
    }
)

#: Aggregation an adapter must not perform when it is supposed to be *reading*
#: a number production publishes. Counting the backlog in the adapter satisfies
#: "the reading matches reality" by construction — the two sides of that
#: comparison become the same expression — while nothing outside the process
#: can see the number at all.
BACKLOG_AUTHORING_OPERATIONS = frozenset(
    {
        "aggregate",
        "count",
    }
)

TRANSITION_AUTHORING_OPERATIONS = frozenset(
    {
        "bulk_create",
        "bulk_update",
        "create",
        "delete",
        "get_or_create",
        "save",
        "update",
        "update_or_create",
    }
)

# Invocation callbacks may select and then mutate. Checking only write method
# names would let a test own half of a transition (for example, choose the row
# with ``filter`` and delegate only the final save), which is still a second
# semantic authority.
INVOCATION_AUTHORING_OPERATIONS = SELECTION_AUTHORING_OPERATIONS | TRANSITION_AUTHORING_OPERATIONS

#: Names whose presence in a test-authored detect/extra probe means the probe
#: inverts or swallows an assertion locally. Inversion is a legitimate need —
#: a decline IS "the discriminating proof fails here" — but it belongs in a
#: root-owned probe (``gap_probes.DisprovenCapability``) that first proves the
#: bindings it feeds are production-bound. A test-side ``pytest.raises`` around
#: a shared proof can "prove" anything by feeding the proof a synthetic
#: implementation and celebrating the failure. ``AssertionError`` counts only
#: where a handler catches it (naming it, ``Exception`` or ``BaseException``, or
#: a bare ``except``; see :func:`_swallowing_handlers`): *raising* it is what
#: every ``assert`` does once pytest rewrites the test module, so its bare name
#: proves nothing. ``suppress`` and ``raises`` count under any imported name.
#:
#: Not covered, so a determined probe can still swallow a proof: a context
#: manager of its own whose ``__exit__`` returns True, a callback pushed onto an
#: ``ExitStack`` that does, a thread or ``sys.excepthook``-style handler that
#: runs the proof and drops its error, and anything else that decides at run
#: time. Neither is suppress or pytest.raises called with a type that cannot
#: catch an assertion told apart: ``suppress(FileNotFoundError)`` is flagged. The
#: tripwire catches the shapes an author reaches for while writing a probe; the
#: backstop is review, and ``DisprovenCapability`` as the one sanctioned inversion.
ASSERTION_INVERSION_NAMES = frozenset({"raises", "AssertionError", "suppress"})


#: The first of these after a load of ``AssertionError`` decides it: a match is an ``except``
#: clause (``except*`` included), a raise is what pytest's rewritten ``assert`` compiles to.
_EXCEPTION_MATCH_OR_RAISE = frozenset({"CHECK_EXC_MATCH", "CHECK_EG_MATCH", "RAISE_VARARGS"})

#: The objects behind the inversion names, so an aliased import (``suppress as quietly``) is seen too.
_INVERTING_OBJECTS: dict[str, object] = {"suppress": contextlib.suppress, "raises": pytest.raises}

#: What static reading returns for a value it cannot determine without running the probe.
_UNRESOLVED = object()


def _nested_code(code: CodeType) -> Iterator[CodeType]:
    """``code`` and every function, lambda and comprehension defined inside it, at any depth."""
    yield code
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from _nested_code(constant)


def _catches_assertion_error_in_bytecode(code: CodeType) -> bool:
    """
    The fallback when the source cannot say: ``AssertionError`` loaded and then tested by an ``except`` clause.

    It sees only the literal name ``AssertionError``: a handler whose type is
    computed (a call, a name the source never binds) and that catches through
    ``Exception``, ``BaseException`` or an alias is invisible to it. It runs only
    for handlers the static reading could not resolve, and for code without
    source, so it can add a finding but never clears one.
    """
    for nested in _nested_code(code):
        instructions = list(dis.get_instructions(nested))
        for index, instruction in enumerate(instructions):
            if instruction.argval != "AssertionError" or not instruction.opname.startswith("LOAD_"):
                continue
            following = (later.opname for later in instructions[index + 1 :])
            decider = next((name for name in following if name in _EXCEPTION_MATCH_OR_RAISE), None)
            if decider in ("CHECK_EXC_MATCH", "CHECK_EG_MATCH"):
                return True
    return False


def _verdict(verdicts: Iterable[bool | None]) -> bool | None:
    """True when any catches, else None when any is unknown, else False; nothing to judge is unknown."""
    found = list(verdicts)
    if any(found):
        return True
    return None if None in found or not found else False


def _value_catches_assertion_error(value: object) -> bool | None:
    """Whether an ``except`` naming ``value`` catches ``AssertionError``; None when ``value`` is not a handler type."""
    if isinstance(value, tuple):
        return _verdict(_value_catches_assertion_error(element) for element in value)
    if isinstance(value, type) and issubclass(value, BaseException):
        return issubclass(AssertionError, value)
    return None


def _imported(module: str, name: str | None = None) -> object:
    """An already-imported module, or a name in one, read without importing or running anything."""
    found = sys.modules.get(module)
    if found is None or name is None:
        return _UNRESOLVED if found is None else found
    value = inspect.getattr_static(found, name, _UNRESOLVED)
    return sys.modules.get(f"{module}.{name}", _UNRESOLVED) if value is _UNRESOLVED else value


def _bindings(node: ast.AST) -> Iterator[tuple[str, ast.expr | object]]:
    """The names one statement or expression binds, each with what it binds it to."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _paired(target, node.value)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
        # ``errors += (AssertionError,)`` may make ``errors`` catch; the addend alone decides that.
        yield from _paired(node.target, node.value)
    elif isinstance(node, ast.NamedExpr):
        yield node.target.id, node.value
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                yield alias.asname, _imported(alias.name)
            else:
                yield alias.name.partition(".")[0], _imported(alias.name.partition(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name != "*":
                found = _imported(node.module, alias.name) if node.module and not node.level else _UNRESOLVED
                yield alias.asname or alias.name, found
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.withitem)):
        target = node.target if isinstance(node, (ast.For, ast.AsyncFor)) else node.optional_vars
        for name in ast.walk(target) if target is not None else ():
            if isinstance(name, ast.Name):
                yield name.id, _UNRESOLVED


def _paired(target: ast.expr, value: ast.expr) -> Iterator[tuple[str, ast.expr | object]]:
    if isinstance(target, ast.Name):
        yield target.id, value
    elif isinstance(target, (ast.Tuple, ast.List)):
        pairs = isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts)
        for index, element in enumerate(target.elts):
            yield from _paired(element, value.elts[index]) if pairs else _unknown(element)  # type: ignore[attr-defined]


def _unknown(target: ast.expr) -> Iterator[tuple[str, object]]:
    for name in ast.walk(target):
        if isinstance(name, ast.Name):
            yield name.id, _UNRESOLVED


class _StaticScope:
    """
    What the names in one probe's source can be bound to, read without running it.

    The namespace is what the code resolves at run time: builtins, its module's
    globals, its closure, its parameters' defaults and a bound method's ``self``.
    Every assignment, walrus, import and loop target in the source is added on
    top, in every nested scope and whatever the order: a name bound to a
    catching type anywhere counts as catching everywhere it is used. The
    over-approximation errs toward flagging, which a probe's author answers by
    moving the inversion root-side (``DisprovenCapability``); erring the other
    way would let a swallowed proof through.
    """

    def __init__(self, tree: ast.Module, namespace: Mapping[str, object]) -> None:
        self._namespace = namespace
        self._bound: dict[str, list[ast.expr | object]] = {}
        for node in ast.walk(tree):
            for name, value in _bindings(node):
                self._bound.setdefault(name, []).append(value)
        self._resolving: set[str] = set()

    def catches(self, expression: ast.expr) -> bool | None:
        """True when a handler of this type catches ``AssertionError``, False when not, None when unknown."""
        if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
            return _verdict(self.catches(element) for element in expression.elts)
        if isinstance(expression, ast.Starred):
            return self.catches(expression.value)
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            return _verdict((self.catches(expression.left), self.catches(expression.right)))
        if isinstance(expression, ast.Name):
            with self._candidates(expression.id) as candidates:
                return _verdict(
                    self.catches(value) if isinstance(value, ast.expr) else _value_catches_assertion_error(value)
                    for value in candidates
                )
        return _verdict(_value_catches_assertion_error(value) for value in self.resolve(expression))

    def resolve(self, expression: ast.expr) -> list[object]:
        """Every value the expression may evaluate to, :data:`_UNRESOLVED` where static reading cannot tell."""
        if isinstance(expression, ast.Constant):
            return [expression.value]
        if isinstance(expression, ast.Name):
            with self._candidates(expression.id) as candidates:
                return [
                    found
                    for value in candidates
                    for found in (self.resolve(value) if isinstance(value, ast.expr) else [value])
                ]
        if isinstance(expression, ast.Attribute):
            # Read without running a descriptor or ``__getattr__``: a property is not a handler type.
            return [
                _UNRESOLVED if base is _UNRESOLVED else inspect.getattr_static(base, expression.attr, _UNRESOLVED)
                for base in self.resolve(expression.value)
            ]
        if isinstance(expression, ast.Subscript) and isinstance(expression.slice, ast.Constant):
            return [_item(base, expression.slice.value) for base in self.resolve(expression.value)]
        if isinstance(expression, (ast.Tuple, ast.List)):
            elements = [self.resolve(element) for element in expression.elts]
            return [tuple(e[0] for e in elements)] if all(len(e) == 1 for e in elements) else [_UNRESOLVED]
        return [_UNRESOLVED]

    @contextlib.contextmanager
    def _candidates(self, name: str) -> Iterator[list[ast.expr | object]]:
        """What ``name`` may be bound to, with the name held while they are read (a self-reference is unknown)."""
        if name in self._resolving:
            # ``errors = errors + (...)``: the earlier value is whatever else binds the name.
            yield [_UNRESOLVED]
        elif name in self._bound:
            self._resolving.add(name)
            try:
                yield self._bound[name]
            finally:
                self._resolving.discard(name)
        else:
            yield [self._namespace[name]] if name in self._namespace else [_UNRESOLVED]


def _item(container: object, key: object) -> object:
    if isinstance(container, (dict, tuple, list)):
        try:
            return container[key]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return _UNRESOLVED
    return _UNRESOLVED


def _swallowing_handlers(code: CodeType, tree: ast.Module | None, namespace: Mapping[str, object]) -> list[int]:
    """
    The source lines of handlers in ``code`` (or code nested in it, at any depth) that swallow ``AssertionError``.

    A handler swallows when it is a bare ``except``, or an ``except``/``except*``
    whose type catches ``AssertionError`` (``AssertionError``, ``Exception`` or
    ``BaseException``, through any alias :class:`_StaticScope` can read), and it
    does not end in a bare ``raise`` with nothing leaving it earlier. Raising
    ``AssertionError`` is what every ``assert`` does once pytest rewrites the
    module, so the bare name proves nothing. When a handler's type cannot be
    read statically, and for code without source, the bytecode decides; its
    finding has no line (0).
    """
    if tree is None:
        return [0] if _catches_assertion_error_in_bytecode(code) else []
    scope = _StaticScope(tree, namespace)
    swallowing: list[int] = []
    unresolved = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        for handler in node.handlers:
            if _re_raises(handler):
                continue
            verdict = True if handler.type is None else scope.catches(handler.type)
            if verdict:
                swallowing.append(_line(code, handler))
            unresolved = unresolved or verdict is None
    if not swallowing and unresolved and _catches_assertion_error_in_bytecode(code):
        return [0]
    return sorted(swallowing)


def _line(code: CodeType, node: ast.stmt | ast.ExceptHandler) -> int:
    """The file line of a node in the dedented source parsed from ``code``."""
    return code.co_firstlineno + node.lineno - 1


def _site(kind: str, code: CodeType, line: int) -> str:
    return f"{kind} at {Path(code.co_filename).name}:{line}" if line else f"{kind} in {code.co_qualname}"


def _inversion_names(code: CodeType, namespace: Mapping[str, object]) -> set[str]:
    """
    The inversions a probe's code makes, each named as a reviewer can find it.

    ``co_names`` holds attribute names too, so ``contextlib.suppress`` counts by
    name as well as by object; a module that rebinds ``suppress`` to something
    harmless is still flagged, the safe direction.
    """
    names = {name for nested in _nested_code(code) for name in nested.co_names}
    found = names & (ASSERTION_INVERSION_NAMES - {"AssertionError"})
    found |= {
        name
        for name, inverter in _INVERTING_OBJECTS.items()
        if any(namespace.get(bound) is inverter for bound in names)
    }
    tree = _source_tree(code)
    if tree is not None and (escapes := _finally_escapes(tree)):
        # A return, break or continue leaving a finally discards the exception in flight, whatever was caught.
        found |= {"finally", "AssertionError"} | {_site("finally", code, _line(code, node)) for node in escapes}
    elif swallowing := _swallowing_handlers(code, tree, namespace):
        found |= {"AssertionError"} | {_site("except", code, line) for line in swallowing}
    return found


def _source_tree(code: CodeType) -> ast.Module | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(code)))
    except (OSError, TypeError, SyntaxError):
        return None


def _function_of(binding: Callable[..., Any]) -> FunctionType | None:
    """
    The Python function behind a binding, or None when there is none.

    The one reading of a binding's shape every tripwire shares: through
    ``functools.partial`` layers, bound methods, decorators that set
    ``__wrapped__``, and a callable instance's ``__call__``.
    """
    while isinstance(binding, functools.partial):
        binding = binding.func
    function = getattr(binding, "__func__", binding)
    if callable(function):
        function = inspect.unwrap(function)
    if inspect.isfunction(function):
        return function
    call = getattr(type(binding), "__call__", None)  # noqa: B004 - reads the class attribute, not callability
    call = getattr(call, "__func__", call)
    return call if inspect.isfunction(call) else None


def _namespace(binding: Callable[..., Any]) -> Mapping[str, object]:
    """
    The names a binding's code resolves at run time, as far as they are known without running it.

    Builtins, then the module's globals, the closure's cells, the parameters'
    defaults, and for a bound method its ``self`` (so ``self.Errors`` resolves).
    """
    function = _function_of(binding)
    if function is None:
        return vars(builtins)
    namespace: dict[str, object] = {**vars(builtins), **function.__globals__}
    with contextlib.suppress(ValueError):  # a cell not yet filled
        namespace.update(inspect.getclosurevars(function).nonlocals)
    parameters = list(inspect.signature(function).parameters.values())
    namespace.update({p.name: p.default for p in parameters if p.default is not inspect.Parameter.empty})
    while isinstance(binding, functools.partial):
        binding = binding.func
    if isinstance(binding, MethodType) and parameters:
        namespace[parameters[0].name] = binding.__self__
    return namespace


def _finally_escapes(tree: ast.Module) -> list[ast.stmt]:
    """
    The statements in ``finally`` blocks that can leave them by ``return``, ``break`` or ``continue``.

    Leaving a ``finally`` that way discards the exception propagating through
    it, an assertion included, whatever the handlers above it did. Counted: a
    ``return`` anywhere in the block, and a ``break`` or ``continue`` not inside
    a loop that is itself in the block. Nested functions, lambdas and classes
    are their own scope and are not counted.
    """
    return [
        statement
        for node in ast.walk(tree)
        if isinstance(node, (ast.Try, ast.TryStar))
        for statement in node.finalbody
        if _escapes(statement, in_loop=False)
    ]


def _escapes(node: ast.AST, *, in_loop: bool) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
        return False
    if isinstance(node, ast.Return):
        return True
    if isinstance(node, (ast.Break, ast.Continue)):
        return not in_loop
    loop = isinstance(node, (ast.For, ast.AsyncFor, ast.While))
    return any(
        # A loop's else clause runs after the loop, so a break there leaves the loop's own enclosing scope.
        _escapes(child, in_loop=in_loop or (loop and child not in node.orelse))  # type: ignore[attr-defined]
        for child in ast.iter_child_nodes(node)
    )


def _re_raises(handler: ast.ExceptHandler) -> bool:
    """Whether every way out of the handler is its closing bare ``raise``: no return, break or continue first."""
    last = handler.body[-1]
    leaves = any(_escapes(statement, in_loop=False) for statement in handler.body)
    return isinstance(last, ast.Raise) and last.exc is None and not leaves


def callable_code(binding: Callable[..., Any]) -> CodeType | None:
    """
    The code object behind a binding: a function, a bound method, a wrapped callable,
    a ``functools.partial`` (whose code is the wrapped function's) or a callable instance.
    """
    function = _function_of(binding)
    return function.__code__ if function is not None else None


def is_test_path(path: Path) -> bool:
    """
    Whether a file is test code: under a ``tests`` directory, or named as pytest collects tests.

    The one rule for "test-authored": the binding tripwires apply it to a
    callable's code and the coverage scan to the files it reads, so a module the
    scan treats as production is never test code to the tripwires, or the reverse.
    """
    return "tests" in path.parts or path.name.startswith(("test_", "conftest")) or path.stem.endswith("_test")


def is_test_code(code: CodeType) -> bool:
    return is_test_path(Path(code.co_filename))


def is_test_authored(binding: Callable[..., Any]) -> bool | None:
    """
    Whether a callable was written in test code rather than by production or the harness.

    ``None`` when it has no inspectable code. A test-authored *negative* claim (a
    decline's proof, an exemption's proof that a lost handoff is absorbed) proves
    only itself: a no-op or a copied query makes any loss look harmless.
    """
    code = callable_code(binding)
    if code is None:
        return None
    return is_test_code(code) and not is_harness_owned(code)


def _nested_test_callables(binding: Callable[..., Any]) -> Collection[Callable[..., Any]]:
    function = _function_of(binding)
    if function is None:
        return ()
    closure = inspect.getclosurevars(function)
    referenced = (*closure.nonlocals.values(), *closure.globals.values())
    return tuple(
        value
        for value in referenced
        if callable(value) and (code := callable_code(value)) is not None and is_test_code(code)
    )


def _authored_operations(
    binding: Callable[..., Any],
    forbidden: Collection[str],
    *,
    seen: set[int],
) -> set[str]:
    identity = id(binding)
    if identity in seen:
        return set()
    seen.add(identity)

    code = callable_code(binding)
    # Root-owned code is the trusted layer these tripwires exist to protect,
    # not a place authorship can hide: everything in the harness package is
    # reviewed as invariant/reference code, so it is exempt the same way
    # production code is.
    if code is None or not is_test_code(code) or is_harness_owned(code):
        return set()

    authored = set(code.co_names) & set(forbidden)
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            authored.update(set(constant.co_names) & set(forbidden))
    for nested in _nested_test_callables(binding):
        authored.update(_authored_operations(nested, forbidden, seen=seen))
    return authored


def assert_test_binding_forwards(
    *,
    adopter: str,
    field: str,
    binding: Callable[..., Any],
    forbidden: Collection[str],
    production_shape: str,
) -> None:
    """Reject a semantic callback that authors ORM behavior in test code."""
    code = callable_code(binding)
    assert code is not None, (
        f"{adopter}: {field} is not an inspectable Python callable, so the "
        "contract cannot establish whether it forwards to production or "
        "reconstructs production behavior"
    )
    authored = _authored_operations(binding, forbidden, seen=set())
    assert not authored, (
        f"{adopter}: {field} authors production semantics in test code "
        f"(uses {sorted(authored)}). The generated tests would measure the "
        "adapter's copy and remain green after runtime drift. Extract "
        f"{production_shape}, call it from runtime, and forward to that same "
        "callable here; keep direct ORM access only in fixtures, fault "
        "injection, and observers"
    )


#: The harness's root directory; everything beneath it is root-owned.
_HARNESS_ROOT = Path(__file__).resolve().parent


def is_harness_owned(code: CodeType) -> bool:
    """
    Whether the code lives in the harness package tree (root-owned).

    The harness's own ``tests/`` packages are excluded: their counterfeit
    bindings are test-authored on purpose, and the guards they exercise must
    see them as test code.
    """
    path = Path(code.co_filename).resolve()
    return _HARNESS_ROOT in path.parents and "tests" not in path.relative_to(_HARNESS_ROOT).parts


def _defining_file(value: Any) -> Path | None:
    """The file that defines a referenced value, for functions/classes/modules/instances."""
    if isinstance(value, ModuleType):
        module_file = getattr(value, "__file__", None)
        return Path(module_file) if module_file else None
    code = callable_code(value) if callable(value) else None
    if code is not None:
        return Path(code.co_filename)
    if inspect.isclass(value) or callable(value):
        # A callable instance whose __call__ lives in a library (a task queue's
        # task object, a functools.partial): its *declared* module is the
        # identity that matters, just as a task is identified by its dotted path.
        module = sys.modules.get(getattr(value, "__module__", ""), None)
        module_file = getattr(module, "__file__", None)
        return Path(module_file) if module_file else None
    return None


def _is_production_file(path: Path) -> bool:
    """Code in one of the host's production packages, outside any test directory."""
    parts = path.parts
    if not any(package in parts for package in production_packages()):
        return False
    return not ("tests" in parts or path.name.startswith(("test_", "conftest")))


def _referenced_values(binding: Callable[..., Any]) -> tuple[Any, ...]:
    """Every global/closure value the callable actually references."""
    function = _function_of(binding)
    if function is None:
        return ()
    try:
        closure = inspect.getclosurevars(function)
    except (TypeError, ValueError):
        return ()
    values = (*closure.nonlocals.values(), *closure.globals.values())
    methods = []
    for value in values:
        if callable(value) or isinstance(value, ModuleType):
            continue
        # Resolve named instance methods without running descriptors/properties.
        # Merely referencing a production-owned data instance is insufficient;
        # a referenced method must itself lead to production behavior.
        for name in function.__code__.co_names:
            member = inspect.getattr_static(value, name, None)
            if isinstance(member, (staticmethod, classmethod)):
                member = member.__func__
            if inspect.isfunction(member) or inspect.ismethod(member):
                methods.append(member)
            elif member is None:
                # A proxy resolves the name at call time through its own
                # ``__getattr__``: that method is the code the access runs.
                # Frameworks expose apps and clients this way (a lazily
                # configured application object), so the name is followed
                # through the proxy's class rather than refused.
                dynamic = inspect.getattr_static(type(value), "__getattr__", None)
                if inspect.isfunction(dynamic):
                    methods.append(dynamic)
    return (*values, *methods)


def is_production_value(value: Any) -> bool:
    """Whether a function, class, module or instance's class is defined in one of the host's production packages."""
    defining = _defining_file(value)
    return defining is not None and _is_production_file(defining)


def _references_production_code(binding: Callable[..., Any], *, seen: set[int]) -> bool:
    identity = id(binding)
    if identity in seen:
        return False
    seen.add(identity)
    for value in _referenced_values(binding):
        if is_production_value(value):
            return True
        code = callable_code(value) if callable(value) else None
        if code is not None and is_test_code(code) and _references_production_code(value, seen=seen):
            return True
    return False


def assert_test_binding_delegates_to_production(
    *,
    adopter: str,
    field: str,
    binding: Callable[..., Any],
    production_shape: str,
) -> None:
    """
    Reject a semantic callback that references no production code at all.

    The authorship tripwire (:func:`assert_test_binding_forwards`) catches a
    *copied* production predicate or transition — it looks for ORM authorship.
    It cannot catch the other counterfeit: a test-side implementation that uses
    no ORM at all (an in-memory dict machine, a plain-Python state function)
    and therefore authors nothing while also invoking nothing. Every behavioral
    proof then measures the fake, and stays green forever after production
    changes — the exact false confidence this module exists to remove.

    So the two tripwires are duals and run together on semantic bindings: a
    binding must not *author* the semantics (forwards check) and must actually
    *reach* production code (this check). A binding that is itself defined in
    production code, or in the harness package (the in-memory references the
    harness's own self-tests bind), is exempt: the question only arises for
    adapter code written in a test module.

    Like the authorship check, this is a focused heuristic rather than a proof
    of equivalence: referencing production code does not prove the reference is
    load-bearing. It raises the cost of the counterfeit from "free and
    invisible" to "deliberate and reviewable", which is the standard every
    tripwire here is held to.
    """
    code = callable_code(binding)
    assert code is not None, (
        f"{adopter}: {field} is not an inspectable Python callable, so the "
        "contract cannot establish whether it delegates to production"
    )
    if is_harness_owned(code) or not is_test_code(code):
        return
    assert _references_production_code(binding, seen=set()), (
        f"{adopter}: {field} is test code that references no production "
        f"callable, class, or module, so the proofs would measure a "
        f"test-defined implementation rather than the runtime. Bind "
        f"{production_shape} — the production function itself, or a thin "
        f"closure that calls it. A conforming in-memory implementation passing "
        f"these proofs demonstrates the harness works; it proves nothing about "
        f"the domain"
    )


_ARGUMENT_LOAD_OPCODES = ("LOAD_FAST", "LOAD_DEREF", "LOAD_CLOSURE")


#: The shortest prose reason that can name what owns a state, a binding shape
#: or a product rule.
MINIMUM_REASON_LENGTH = 20


def is_real_reason(reason: str | None) -> bool:
    """A declared reason long enough to name an owner, not blank or a placeholder."""
    return len((reason or "").strip()) >= MINIMUM_REASON_LENGTH


def assert_binding_reaches_production(
    *,
    adopter: str,
    field: str,
    binding: Callable[..., Any],
    forbidden: Collection[str],
    production_shape: str,
) -> None:
    """
    Both halves of "this binding is production": it authors none of
    ``forbidden`` in test code (:func:`assert_test_binding_forwards`) and it
    references production code at all (:func:`assert_test_binding_delegates_to_production`).
    Either half alone admits a counterfeit — a copied query, or a plain-Python
    state machine — so a semantic binding is checked with both.
    """
    assert_test_binding_forwards(
        adopter=adopter, field=field, binding=binding, forbidden=forbidden, production_shape=production_shape
    )
    assert_test_binding_delegates_to_production(
        adopter=adopter, field=field, binding=binding, production_shape=production_shape
    )


def assert_test_binding_consumes_its_first_parameter(
    *,
    adopter: str,
    field: str,
    binding: Callable[..., Any],
    parameter_shape: str,
) -> None:
    """
    Reject an adapter callback that ignores the identity it was handed.

    The counterfeit this catches is quiet and reads as correct::

        def execute(row_id: int) -> None:
            SomeService.reconcile_tick()      # row_id never used

    The signature says the operation is about ``row_id``; the body runs
    whatever a production selection happens to find. Every proof downstream
    then measures a different operation from the one the fixture arranged —
    usually one that has already settled, so it does nothing at all.

    Only adapter code is inspected. A binding that *is* a production callable
    necessarily consumes its own parameters, and a harness-owned one is
    reviewed as invariant code; both are exempt, exactly as they are for the
    authorship and delegation tripwires.

    Bytecode rather than source, for the same reason invariant 0a uses
    bytecode: it survives reformatting, comments, and the parameter being
    captured by a nested closure (which loads it through a cell).
    """
    code = callable_code(binding)
    assert code is not None, (
        f"{adopter}: {field} is not an inspectable Python callable, so the "
        f"contract cannot establish whether it consumes {parameter_shape}"
    )
    if is_harness_owned(code) or not is_test_code(code):
        return
    positional = code.co_varnames[: code.co_argcount]
    if not positional:
        return
    parameter = positional[0]
    loaded = {
        instruction.argval
        for instruction in dis.get_instructions(code)
        if instruction.opname.startswith(_ARGUMENT_LOAD_OPCODES)
    }
    assert parameter in loaded, (
        f"{adopter}: {field} never reads its {parameter!r} parameter, so it "
        f"does not act on {parameter_shape}. The proofs would arrange one "
        f"operation and measure whatever this callback found on its own — "
        f"typically nothing, because the arranged one has already settled. "
        f"Pass the identity through to the production call"
    )


def authored_inversion_names(binding: Callable[..., Any], *, seen: set[int] | None = None) -> set[str]:
    """
    Assertion-inversion names a test-authored probe references, recursively.

    Used by the contract layer to refuse ``Decline.prove`` / ``KnownGap.detect``
    bindings that invert a shared proof locally — see
    :data:`ASSERTION_INVERSION_NAMES` for why inversion must live root-side.
    Root-owned callables are trusted and not descended into: a harness proof may
    legitimately raise ``AssertionError`` (that is what a proof is), and
    :class:`~.gap_probes.DisprovenCapability` legitimately inverts one.
    """
    if seen is None:
        seen = set()
    identity = id(binding)
    if identity in seen:
        return set()
    seen.add(identity)

    code = callable_code(binding)
    if code is None or not is_test_code(code) or is_harness_owned(code):
        return set()

    inverted = _inversion_names(code, _namespace(binding))
    for nested in _nested_test_callables(binding):
        inverted.update(authored_inversion_names(nested, seen=seen))
    return inverted


def references_harness_assertion(binding: Callable[..., Any], *, seen: set[int] | None = None) -> bool:
    """
    Whether the callable (or a nested test helper) references a real harness proof.

    The predecessor of this check matched any referenced *name* starting with
    ``assert_`` — so a test module could define its own ``assert_anything`` and
    satisfy it without touching the harness at all. This resolves the referenced
    values instead and requires at least one to be a callable defined in the
    harness package whose name starts with ``assert_``.
    """
    if seen is None:
        seen = set()
    identity = id(binding)
    if identity in seen:
        return False
    seen.add(identity)
    for value in _referenced_values(binding):
        if not callable(value):
            continue
        name = getattr(value, "__name__", "")
        code = callable_code(value)
        if name.startswith("assert_") and code is not None and is_harness_owned(code):
            return True
        if code is not None and is_test_code(code) and references_harness_assertion(value, seen=seen):
            return True
    return False
