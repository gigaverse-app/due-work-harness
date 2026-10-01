"""
Small building blocks beside the declarative contract layer.

Most adopters never call these directly: they declare a
:class:`~pytest_obligation.contract.ObligationContract` and let
:func:`~pytest_obligation.contract.due_work_contract_suite` generate every case.
What lives here is what the generated suites and bespoke harness work share:

* :func:`contract_params` — a profile's proofs as ``pytest.param`` values, with
  known gaps as strict xfails.
* :func:`undeclared` — a binding member an adopter deliberately does not
  supply, carrying the reason to whoever reaches it.
* :func:`assert_provider_call_holds_no_transaction` — a standalone proof that
  no database transaction is open while an external provider is called.
* :func:`wait_until` — one clock around a wait for work another process or
  thread settles, as recovery by a real worker or a restart needs.

For what a passing or failing proof does and does not tell you, see
``docs/what-a-green-result-means.md``.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, TypeVar

import pytest

from pytest_obligation.host import current_host

T = TypeVar("T")


def contract_params(
    proofs: tuple[Callable[..., None], ...],
    *,
    gaps: dict[str, str] | None = None,
) -> list[Any]:
    """
    Parametrise a profile's proofs by hand, with known gaps as strict xfails.

    Low-level building block: adopters declare a
    :class:`~pytest_obligation.contract.ObligationContract` and let
    :func:`~pytest_obligation.contract.due_work_contract_suite` generate this
    shape for every claimed profile at once. This helper remains for bespoke
    harness work and for the harness's own self-tests.

    The shape it produces: run the *whole* contract so each invariant is
    its own test and a failure names the property, and mark the invariants the
    domain is known to fail as ``xfail(strict=True)`` with the reason attached —
    never skipped, never silently absent. ``strict`` is the discipline that
    turns a finding into a tripwire: whoever fixes the production gap is forced
    to delete its entry here, so the contract's verdict and the code can never
    quietly disagree.

    ``gaps`` maps a proof's ``__name__`` to the reason it currently fails. A gap
    naming a proof that is not in ``proofs`` is an error rather than ignored:
    that is what happens when a proof is renamed, and dropping the mark silently
    would resurface the failure as a hard red with its explanation lost.

    The marks here cover only the xfail discipline; add whatever your host
    needs for database access (``current_host().database_marks(...)``) to the
    test yourself. Usage::

        @pytest.mark.parametrize(
            "proof",
            contract_params(DUE_WORK_PROOFS, gaps={
                "assert_selection_is_index_served": "no index serves ...",
            }),
        )
        def test_my_sweep_satisfies_the_due_work_contract(proof, ...):
            proof(_my_sweep())
    """
    remaining = dict(gaps or {})
    params = []
    for proof in proofs:
        reason = remaining.pop(proof.__name__, None)
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        params.append(pytest.param(proof, marks=marks, id=proof.__name__))
    assert not remaining, (
        f"gaps name proofs that are not in this contract: {sorted(remaining)}. "
        f"If a proof was renamed, rename its gap entry with it — dropping the "
        f"mark would turn a documented gap into an unexplained failure"
    )
    return params


def undeclared(because: str) -> Callable[..., Any]:
    """
    A profile member this adopter does not supply, with the reason attached.

    Most adopters are partial on purpose — an adopter may apply only
    :func:`~pytest_obligation.profiles.automatic_recovery.assert_selection_is_index_served`,
    for a stated reason: the behavioural proofs need a populated object graph,
    or the domain runs its work inline rather than fanning out to a per-row
    worker, or another suite already covers those paths in more depth. Without
    this helper, every such adopter hand-rolls the same three-line stub to say
    so.

    The reason this is worth a helper rather than a copied stub is what happens
    when someone *does* broaden an adopter later. A local ``unsupported`` says
    nothing about which member was reached; this names it, and carries the
    original author's explanation of why it was left out to whoever hits it.

    Use it for callable members. For ``grace`` and ``page_size``, pass ``None``:
    an adopter that had to invent a number is the failure this pairs with. A
    fabricated ``page_size=0`` in an adopter whose own documentation says there
    is no page size to declare is inert only until someone applies a proof that
    reads it.
    """

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError(
            f"this adopter declared the member undeclared, and a proof reached it anyway: {because}"
        )

    return refuse


def assert_provider_call_holds_no_transaction(
    *,
    name: str,
    run_with_probe: Callable[[Callable[[], None]], Any],
) -> None:
    """
    A provider call must not run inside an open database transaction.

    Holding a transaction across a network call keeps row locks and a pooled
    connection for the provider's entire latency, including its timeout. Under
    load that converts one slow provider into database-wide lock contention and
    connection-pool exhaustion, and it is invisible in every test that stubs the
    provider to return instantly.

    ``run_with_probe`` must invoke ``probe()`` at the exact point the provider
    would be called — usually by substituting the provider or the I/O helper.
    Whether a transaction is open is a fact only the application's framework
    knows, so the proof asks the host's ``in_transaction`` capability.

    The calling test must not hold a transaction of its own. A test setup that
    wraps every test in a transaction (the usual non-transactional database
    mark) makes the host report a transaction open unconditionally, and this
    proof would report a violation for code that has none. Run it as an
    :class:`~pytest_obligation.contract.ExtraProof` with ``transactional=True``,
    or give a hand-written test the host's transactional database marks. That
    false positive is guarded below rather than documented, because a proof
    that can be wrong in the accusing direction is worse than no proof.

    Durable-execution runtimes commonly enforce this rule internally; it
    generalises to hand-written background work, which is why it is a
    standalone proof here rather than a property of any one runtime.
    """
    in_transaction: Callable[[], bool] = current_host().require("in_transaction")
    assert not in_transaction(), (
        f"{name}: the host reports a transaction open before the code under test ran, so the test "
        f"itself holds one and every reading would be a false violation. Run this proof in a test "
        f"with no test-wrapping transaction: ExtraProof(transactional=True), or the host's "
        f"database_marks(True) on a hand-written test"
    )

    observed: list[bool] = []

    def probe() -> None:
        observed.append(in_transaction())

    run_with_probe(probe)

    assert observed, f"{name}: the probe was never called, so this proof did not observe the provider boundary at all"
    assert not any(observed), (
        f"{name}: the host reports a transaction open during the provider call. Row locks and "
        f"a pooled connection are held for the provider's entire latency, so one "
        f"slow provider becomes database-wide contention"
    )


def wait_until(settled: Callable[[], bool], *, timeout: float = 60.0, what: str, poll: float = 0.2) -> None:
    """
    Poll ``settled`` until it holds, failing with ``what`` once ``timeout`` seconds pass.

    For recovery that runs somewhere a test cannot step: a worker in a child
    process, a workflow engine's own threads, an application restarted.
    """
    deadline = time.monotonic() + timeout
    while not settled():
        assert time.monotonic() < deadline, f"{what} within {timeout}s"
        time.sleep(poll)


@contextmanager
def proof_context(scope: AbstractContextManager[T]) -> Iterator[T]:
    """Enter adopter resources without allowing cleanup to turn a failed proof green.

    The original exception still reaches __exit__ so rollback and cleanup keep
    their normal semantics. If the context suppresses it, re-raise that same
    exception afterwards, including its replay notes and invariant identity.
    """
    failure: BaseException | None = None
    with scope as binding:
        try:
            yield binding
        except BaseException as error:
            failure = error
            raise
    if failure is not None:
        raise failure
