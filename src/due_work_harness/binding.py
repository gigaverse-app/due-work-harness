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
from collections.abc import Callable, Collection, Iterator, Mapping
from pathlib import Path
from types import CodeType, MethodType, ModuleType
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
#: a bare ``except``; see :func:`_catches_assertion_error`): *raising* it is what
#: every ``assert`` does once pytest rewrites the test module, so its bare name
#: proves nothing. ``suppress`` and ``raises`` count under any imported name.
ASSERTION_INVERSION_NAMES = frozenset({"raises", "AssertionError", "suppress"})


#: Exception types whose handler also catches the ``AssertionError`` a failing proof raises.
_ASSERTION_CATCHING_TYPES = frozenset({"AssertionError", "Exception", "BaseException"})

#: The first of these after a load of ``AssertionError`` decides it: a match is an ``except``
#: clause (``except*`` included), a raise is what pytest's rewritten ``assert`` compiles to.
_EXCEPTION_MATCH_OR_RAISE = frozenset({"CHECK_EXC_MATCH", "CHECK_EG_MATCH", "RAISE_VARARGS"})

#: The objects behind the inversion names, so an aliased import (``suppress as quietly``) is seen too.
_INVERTING_OBJECTS: dict[str, object] = {"suppress": contextlib.suppress, "raises": pytest.raises}


def _nested_code(code: CodeType) -> Iterator[CodeType]:
    """``code`` and every function, lambda and comprehension defined inside it, at any depth."""
    yield code
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from _nested_code(constant)


def _catches_assertion_error_in_bytecode(code: CodeType) -> bool:
    """The fallback when the source cannot say: ``AssertionError`` tested by an ``except`` clause."""
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


def _catches_assertion_error(code: CodeType, tree: ast.Module | None, namespace: Mapping[str, object]) -> bool:
    """
    Whether ``code`` (or a function nested in it, at any depth) has a handler that swallows ``AssertionError``.

    Read from the source: a bare ``except``, or an ``except``/``except*`` naming
    ``AssertionError``, ``Exception`` or ``BaseException`` directly, in a tuple,
    starred, or through a name bound to one of them locally (plain or annotated
    assignment) or in ``namespace`` (the probe's module globals over the
    builtins). A handler that ends in a bare ``raise``, with nothing leaving it
    earlier, re-raises, so it swallows nothing. Raising ``AssertionError`` is
    what every ``assert`` does once pytest rewrites the module, so the bare name
    proves nothing. A handler type the source cannot resolve (a call) and code
    without source are decided by the bytecode's exception matches.
    """
    if tree is None:
        return _catches_assertion_error_in_bytecode(code)
    catching = set(_ASSERTION_CATCHING_TYPES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _handler_type_catches(node.value, catching, namespace):
            catching.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            if _handler_type_catches(node.value, catching, namespace):
                catching.add(node.target.id)
    unresolved = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        for handler in node.handlers:
            if _re_raises(handler):
                continue
            verdict = True if handler.type is None else _handler_type_catches(handler.type, catching, namespace)
            if verdict:
                return True
            unresolved = unresolved or verdict is None
    return unresolved and _catches_assertion_error_in_bytecode(code)


def _handler_type_catches(expression: ast.expr, names: Collection[str], namespace: Mapping[str, object]) -> bool | None:
    """True when the handler type catches ``AssertionError``, False when it cannot, None when the source cannot say."""
    if isinstance(expression, ast.Tuple):
        verdicts = [_handler_type_catches(element, names, namespace) for element in expression.elts]
        return True if any(verdicts) else None if None in verdicts else False
    if isinstance(expression, ast.Starred):
        return _handler_type_catches(expression.value, names, namespace)
    if isinstance(expression, ast.Attribute):
        return expression.attr in names
    if not isinstance(expression, ast.Name):
        return None
    if expression.id in names:
        return True
    if expression.id not in namespace:
        return None
    return _value_catches_assertion_error(namespace[expression.id])


def _value_catches_assertion_error(value: object) -> bool | None:
    """Whether an ``except`` naming ``value`` catches ``AssertionError``; None when ``value`` is not a handler type."""
    if isinstance(value, tuple):
        verdicts = [_value_catches_assertion_error(element) for element in value]
        return True if any(verdicts) else None if None in verdicts else False
    if isinstance(value, type) and issubclass(value, BaseException):
        return issubclass(AssertionError, value)
    return None


def _inversion_names(code: CodeType, namespace: Mapping[str, object]) -> set[str]:
    names = {name for nested in _nested_code(code) for name in nested.co_names}
    found = names & (ASSERTION_INVERSION_NAMES - {"AssertionError"})
    found |= {
        name
        for name, inverter in _INVERTING_OBJECTS.items()
        if any(namespace.get(bound) is inverter for bound in names)
    }
    tree = _source_tree(code)
    if tree is not None and _leaves_a_finally_early(tree):
        # A return, break or continue leaving a finally discards the exception in flight, whatever was caught.
        found |= {"finally", "AssertionError"}
    elif _catches_assertion_error(code, tree, namespace):
        found.add("AssertionError")
    return found


def _source_tree(code: CodeType) -> ast.Module | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(code)))
    except (OSError, TypeError, SyntaxError):
        return None


def _namespace(binding: Callable[..., Any]) -> Mapping[str, object]:
    """The names a binding's code resolves at run time: its module's globals over the builtins."""
    while isinstance(binding, functools.partial):
        binding = binding.func
    function = inspect.unwrap(binding.__func__ if isinstance(binding, MethodType) else binding)
    if not inspect.isfunction(function):
        function = type(binding).__call__
    module_globals = function.__globals__ if inspect.isfunction(function) else {}
    return {**vars(builtins), **module_globals}


def _leaves_a_finally_early(tree: ast.Module) -> bool:
    """
    Whether some ``finally`` block can leave by ``return``, ``break`` or ``continue``.

    Leaving a ``finally`` that way discards the exception propagating through
    it, an assertion included, whatever the handlers above it did. Counted: a
    ``return`` anywhere in the block, and a ``break`` or ``continue`` not inside
    a loop that is itself in the block. Nested functions, lambdas and classes
    are their own scope and are not counted.
    """
    return any(
        _escapes(statement, in_loop=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Try, ast.TryStar))
        for statement in node.finalbody
    )


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
    if isinstance(binding, functools.partial):
        return callable_code(binding.func)
    function = getattr(binding, "__func__", binding)
    if callable(function):
        function = inspect.unwrap(function)
    code = getattr(function, "__code__", None)
    if code is None:
        code = getattr(getattr(type(binding), "__call__", None), "__code__", None)  # noqa: B004 - reads the class attribute, not callability
    return code


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
    function = getattr(binding, "__func__", binding)
    if callable(function):
        function = inspect.unwrap(function)
    if not inspect.isfunction(function):
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
    function = getattr(binding, "__func__", binding)
    if callable(function):
        function = inspect.unwrap(function)
    if not inspect.isfunction(function):
        call = getattr(type(binding), "__call__", None)  # noqa: B004 - reads the class attribute, not callability
        function = getattr(call, "__func__", call)
        if not inspect.isfunction(function):
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
