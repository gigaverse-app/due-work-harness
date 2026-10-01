"""
The inversion tripwire: which test-authored probes swallow a proof's assertion, read from source and names.

A probe inverts an assertion by catching it (an ``except`` naming ``AssertionError``,
``Exception`` or ``BaseException``, directly, in a tuple, starred, or through a name
bound to one locally, in its module or in the builtins; a bare ``except``), by
suppressing or expecting it (``contextlib.suppress``, ``pytest.raises``, under any
imported name), or by leaving a ``finally`` early. A handler that ends in a bare
``raise`` with no way out before it swallows nothing; neither does a plain ``assert``,
which pytest rewrites into a raise.
"""

import builtins
import contextlib
import functools
import inspect
from contextlib import suppress as quietly
from typing import Any

import pytest
from pytest import raises as expect_failure

from pytest_obligation.binding import authored_inversion_names, callable_code


def _probe_that_swallows(proof: int) -> None:
    with contextlib.suppress(AssertionError):
        assert proof == 0


def _probe_that_catches(proof: int) -> None:
    try:
        assert proof == 0
    except AssertionError:
        return


def _probe_that_only_asserts(proof: int) -> None:
    # pytest rewrites this into a raise of AssertionError, which is what an assertion is.
    assert proof == 0


def _probe_that_asserts_and_swallows_something_else(proof: int) -> None:
    try:
        assert proof == 0
    except KeyError:
        return


def _probe_that_catches_a_group(proof: int) -> None:
    try:
        assert proof == 0
    except* AssertionError:
        pass


def _probe_that_catches_through_an_alias(proof: int) -> None:
    caught = AssertionError
    assert proof is not None
    try:
        assert proof == 0
    except caught:
        return


def _probe_that_catches_everything(proof: int) -> None:
    try:
        assert proof == 0
    except Exception:  # noqa: BLE001 - the inversion under test
        return


def _probe_that_passes_on_everything(proof: int) -> None:
    try:
        assert proof == 0
    except Exception:  # noqa: BLE001, S110 - the inversion under test
        pass


def _probe_that_catches_even_base_exceptions(proof: int) -> None:
    try:
        assert proof == 0
    except BaseException:  # noqa: BLE001 - the inversion under test
        return


def _probe_that_catches_base_exceptions_in_a_tuple(proof: int) -> None:
    try:
        assert proof == 0
    except (ValueError, BaseException):  # noqa: BLE001 - the inversion under test
        return


def _probe_with_a_bare_except(proof: int) -> None:
    try:
        assert proof == 0
    except:  # noqa: E722 - the inversion under test
        return


def _probe_that_swallows_two_functions_deep(proof: int) -> None:
    def outer() -> None:
        def inner() -> None:
            with contextlib.suppress(AssertionError):
                assert proof == 0

        inner()

    outer()


def _probe_that_catches_a_starred_tuple(proof: int) -> None:
    errors = (AssertionError,)
    try:
        assert proof == 0
    except (ValueError, *errors):
        return


def _probe_that_catches_a_looked_up_type(proof: int) -> None:
    try:
        assert proof == 0
    except getattr(builtins, "AssertionError"):  # noqa: B009 - the disguise under test
        return


def _probe_that_catches_through_an_annotated_alias(proof: int) -> None:
    caught: type[BaseException] = AssertionError
    try:
        assert proof == 0
    except caught:
        return


def _probe_that_notes_and_reraises(proof: int) -> None:
    try:
        assert proof == 0
    except Exception as error:  # noqa: BLE001 - catch-and-re-raise, which does not invert
        error.add_note("while probing")
        raise


def _probe_that_returns_from_finally(proof: int) -> None:
    try:
        assert proof == 0
    finally:
        return  # noqa: B012 - the inversion under test: it discards the exception


def _probe_that_reraises_inside_a_swallowing_finally(proof: int) -> None:
    for _ in range(1):
        try:
            assert proof == 0
        except AssertionError:
            raise
        finally:
            break  # noqa: B012 - the inversion under test


def _probe_that_breaks_from_a_loop_else_in_finally(proof: int) -> None:
    for _ in range(1):
        try:
            assert proof == 0
        finally:
            for _ in ():
                pass
            else:
                break  # noqa: B012 - the else leaves the finally


def _probe_whose_finally_breaks_only_its_own_loop(proof: int) -> None:
    try:
        assert proof == 0
    finally:
        for _ in range(1):
            break


_MODULE_ALIAS = AssertionError
_MODULE_TUPLE = (ValueError, AssertionError)
Fail = AssertionError


def _probe_that_swallows_on_one_branch_then_reraises(proof: int) -> None:
    try:
        assert proof == 0
    except AssertionError as error:
        if "gap still open" in str(error):
            return
        raise


def _probe_that_returns_before_its_reraise(proof: int) -> None:
    try:
        assert proof == 0
    except Exception:  # noqa: BLE001 - the inversion under test
        return
        raise  # noqa: PLE0704 - unreachable on purpose: the raise is not the handler's exit


def _probe_that_continues_before_its_reraise(proof: int) -> None:
    for _ in range(1):
        try:
            assert proof == 0
        except AssertionError:
            continue
            raise


def _make_a_closure_probe() -> Any:
    caught = AssertionError

    def probe(proof: int) -> None:
        try:
            assert proof == 0
        except caught:
            return

    return probe


class _Holder:
    Errors = (AssertionError,)

    def probe(self, proof: int) -> None:
        try:
            assert proof == 0
        except self.Errors:
            return


def _probe_that_catches_a_locally_imported_alias(proof: int) -> None:
    from builtins import AssertionError as Caught  # noqa: A004 - the disguise under test

    try:
        assert proof == 0
    except Caught:
        return


def _probe_that_catches_a_walrus_alias(proof: int) -> None:
    if (caught := AssertionError) is not None:
        try:
            assert proof == 0
        except caught:
            return


def _probe_that_catches_a_concatenated_tuple(proof: int) -> None:
    errors = (ValueError,) + (AssertionError,)
    try:
        assert proof == 0
    except errors:
        return


_ERRORS = {"assert": AssertionError}


def _probe_that_catches_a_subscripted_type(proof: int) -> None:
    try:
        assert proof == 0
    except _ERRORS["assert"]:
        return


def _probe_that_catches_through_a_default(proof: int, caught: type[BaseException] = AssertionError) -> None:
    try:
        assert proof == 0
    except caught:
        return


def _probe_that_catches_pytest_skip(proof: int) -> None:
    try:
        assert proof == 0
    except pytest.skip.Exception:  # Skipped is no AssertionError catcher
        return


class _NotAnAssertion(ValueError):
    pass


def _probe_that_catches_its_own_value_error(proof: int) -> None:
    try:
        assert proof == 0
    except _NotAnAssertion:
        return


def _probe_that_catches_a_module_alias(proof: int) -> None:
    try:
        assert proof == 0
    except _MODULE_ALIAS:
        return


def _probe_that_catches_a_capitalised_module_alias(proof: int) -> None:
    try:
        assert proof == 0
    except Fail:
        return


def _probe_that_catches_a_module_tuple(proof: int) -> None:
    try:
        assert proof == 0
    except _MODULE_TUPLE:
        return


def _probe_that_suppresses_through_an_aliased_import(proof: int) -> None:
    with quietly(AssertionError):
        assert proof == 0


def _probe_that_expects_the_failure_through_an_aliased_import(proof: int) -> None:
    with expect_failure(AssertionError):
        assert proof == 0


@pytest.mark.parametrize(
    ("probe", "inverts"),
    [
        pytest.param(_probe_that_swallows, True, id="suppress"),
        pytest.param(_probe_that_catches, True, id="except-AssertionError"),
        pytest.param(_probe_that_catches_a_group, True, id="except-star"),
        pytest.param(_probe_that_catches_through_an_alias, True, id="aliased-type"),
        pytest.param(_probe_that_catches_everything, True, id="except-Exception"),
        pytest.param(_probe_that_passes_on_everything, True, id="except-Exception-pass"),
        pytest.param(_probe_that_catches_even_base_exceptions, True, id="except-BaseException"),
        pytest.param(_probe_that_catches_base_exceptions_in_a_tuple, True, id="tuple-with-BaseException"),
        pytest.param(_probe_with_a_bare_except, True, id="bare-except"),
        pytest.param(_probe_that_swallows_two_functions_deep, True, id="nested-two-deep"),
        pytest.param(_probe_that_catches_a_starred_tuple, True, id="starred-tuple"),
        pytest.param(_probe_that_catches_a_looked_up_type, True, id="looked-up-type"),
        pytest.param(_probe_that_catches_through_an_annotated_alias, True, id="annotated-alias"),
        pytest.param(_probe_that_notes_and_reraises, False, id="catch-and-reraise"),
        pytest.param(_probe_that_returns_from_finally, True, id="return-in-finally"),
        pytest.param(_probe_that_breaks_from_a_loop_else_in_finally, True, id="break-in-a-loop-else-in-finally"),
        pytest.param(_probe_whose_finally_breaks_only_its_own_loop, False, id="finally-breaking-its-own-loop"),
        pytest.param(_probe_that_swallows_on_one_branch_then_reraises, True, id="swallow-on-a-branch-then-reraise"),
        pytest.param(_probe_that_returns_before_its_reraise, True, id="return-before-reraise"),
        pytest.param(_probe_that_continues_before_its_reraise, True, id="continue-before-reraise"),
        pytest.param(_probe_that_catches_a_module_alias, True, id="module-alias"),
        pytest.param(_probe_that_catches_a_capitalised_module_alias, True, id="module-alias-Fail"),
        pytest.param(_probe_that_catches_a_module_tuple, True, id="module-tuple"),
        pytest.param(_probe_that_suppresses_through_an_aliased_import, True, id="aliased-suppress"),
        pytest.param(_probe_that_expects_the_failure_through_an_aliased_import, True, id="aliased-raises"),
        pytest.param(_probe_that_reraises_inside_a_swallowing_finally, True, id="reraise-under-break-in-finally"),
        pytest.param(_make_a_closure_probe(), True, id="closure-alias"),
        pytest.param(_Holder().probe, True, id="instance-attribute"),
        pytest.param(_probe_that_catches_a_locally_imported_alias, True, id="local-import-alias"),
        pytest.param(_probe_that_catches_a_walrus_alias, True, id="walrus-alias"),
        pytest.param(_probe_that_catches_a_concatenated_tuple, True, id="concatenated-tuple"),
        pytest.param(_probe_that_catches_a_subscripted_type, True, id="subscripted-type"),
        pytest.param(_probe_that_catches_through_a_default, True, id="parameter-default"),
        pytest.param(_probe_that_catches_pytest_skip, False, id="pytest-skip-exception"),
        pytest.param(_probe_that_catches_its_own_value_error, False, id="value-error-subclass"),
        pytest.param(_probe_that_only_asserts, False, id="plain-assert"),
        pytest.param(_probe_that_asserts_and_swallows_something_else, False, id="except-other-exception"),
    ],
)
def test_a_probe_inverts_an_assertion_only_by_catching_it(probe: Any, inverts: bool) -> None:
    assert bool(authored_inversion_names(probe)) is inverts


@pytest.mark.parametrize("probe", [_probe_that_swallows, _probe_that_catches], ids=["suppress", "except"])
def test_wrapping_an_inverting_probe_in_a_partial_does_not_hide_it(probe: Any) -> None:
    wrapped = functools.partial(functools.partial(probe))
    assert callable_code(wrapped) is probe.__code__
    assert authored_inversion_names(wrapped) == authored_inversion_names(probe) != set()


def test_the_handler_that_swallows_is_named_by_its_line() -> None:
    lines, _ = inspect.getsourcelines(_probe_that_catches_a_module_alias)
    handler = next(index for index, line in enumerate(lines) if "except _MODULE_ALIAS" in line)
    line = _probe_that_catches_a_module_alias.__code__.co_firstlineno + handler
    assert f"except at test_inversion_tripwire.py:{line}" in authored_inversion_names(
        _probe_that_catches_a_module_alias
    )


def test_a_probe_that_leaves_its_finally_is_named_by_the_finally_line() -> None:
    names = authored_inversion_names(_probe_that_returns_from_finally)
    assert any(name.startswith("finally at test_inversion_tripwire.py:") for name in names), names


class _TaskBody:
    def run(self, proof: int) -> None:
        try:
            assert proof == 0
        except Exception:  # noqa: BLE001 - the inversion under test
            return


class _TaskProxy:
    """A lazily bound task, as Celery's ``shared_task(bind=True)`` returns: its ``__wrapped__`` is a bound ``run``."""

    def __init__(self) -> None:
        self.__wrapped__ = _TaskBody().run

    def __call__(self, proof: int) -> None:
        return self.__wrapped__(proof)


def test_a_proxy_wrapping_a_bound_method_is_read_as_the_method_not_the_proxy() -> None:
    # Unwrapping lands on a bound method: its function is the body the binding runs, not the proxy's __call__.
    assert callable_code(_TaskProxy()) is _TaskBody.run.__code__
    assert "AssertionError" in authored_inversion_names(_TaskProxy())


def _logged(function: Any) -> Any:
    @functools.wraps(function)
    def logging(*args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    return logging


class _DecoratedHolder:
    @_logged
    def probe(self, proof: int) -> None:
        try:
            assert proof == 0
        except Exception:  # noqa: BLE001 - the inversion under test
            return


def test_a_bound_method_whose_function_is_decorated_is_read_as_the_undecorated_body() -> None:
    assert callable_code(_DecoratedHolder().probe) is inspect.unwrap(_DecoratedHolder.probe).__code__
    assert "AssertionError" in authored_inversion_names(_DecoratedHolder().probe)
