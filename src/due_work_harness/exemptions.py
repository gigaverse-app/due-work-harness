"""
Exemptions: a handoff whose loss is acceptable, declared where it is proven.

Some handoffs can be lost without losing anything: a cache eviction bounded by
a TTL, an eager optimization the next request re-derives. Saying so is a claim
about production, so the claim must run::

    @exempt_due_work_suite(
        DueWorkSource(PublicCache.evict_after_commit),
        reason="entries expire within 60 seconds and the database stays authoritative",
        prove=LossIsAbsorbedElsewhere(strand=..., observe=..., absorb=...),
    )
    class TestPublicCacheEvictionExemption:
        pass

The decorator generates ``test_the_exemption_holds``, which runs ``prove``. The
coverage scan (:mod:`due_work_harness.coverage`) finds the declaration
statically, so the exempted function counts as accounted for exactly while this
suite exists — and the suite fails the moment the reason stops being true.
"""

from collections.abc import Callable
from typing import Any

import pytest

from due_work_harness.binding import is_real_reason, is_test_authored
from due_work_harness.contract import DueWorkContractDesignError, DueWorkSource
from due_work_harness.host import current_host


def exempt_due_work_suite(
    source: DueWorkSource,
    *,
    reason: str,
    prove: Callable[[], None],
    transactional: bool = False,
) -> Callable[[type], type]:
    """
    Declare that losing ``source``'s handoff is acceptable, and prove it.

    ``reason`` names what absorbs the loss; ``prove`` runs it, normally
    :class:`~due_work_harness.gap_probes.LossIsAbsorbedElsewhere`, which strands
    the work with its handoff suppressed and requires the production path the
    reason names to produce the effect anyway. ``transactional`` asks the host
    for real commits while the proof runs.
    """
    if not isinstance(source, DueWorkSource):
        raise DueWorkContractDesignError("exempt_due_work_suite takes a DueWorkSource(callable)")
    if not is_real_reason(reason):
        raise DueWorkContractDesignError(
            "an exemption's reason must name what absorbs the lost handoff; a placeholder is not a reason"
        )
    if not callable(prove):
        raise DueWorkContractDesignError(
            "an exemption must carry prove=, executable evidence that the loss is absorbed"
        )
    authored = is_test_authored(prove)
    if authored is None:
        raise DueWorkContractDesignError("an exemption's prove= is not an inspectable Python callable")
    if authored:
        raise DueWorkContractDesignError(
            "an exemption's prove= is test-authored, and a proof the test wrote can make any loss look absorbed: "
            "use a harness probe such as LossIsAbsorbedElsewhere, bound to the production paths the reason names"
        )

    def decorate(cls: type) -> type:
        test_name = "test_the_exemption_holds"
        assert not hasattr(cls, test_name), f"{cls.__name__} already defines {test_name}"

        def test_the_exemption_holds(self: Any) -> None:
            try:
                prove()
            except AssertionError as error:
                error.add_note(f"the exemption for {source.callable.__qualname__} does not hold: {reason}")
                raise

        test_the_exemption_holds.__qualname__ = f"{cls.__qualname__}.{test_name}"
        test_the_exemption_holds.__doc__ = f"Losing {source.callable.__qualname__}'s handoff is absorbed: {reason}"
        test: Any = pytest.mark.due_work(test_the_exemption_holds)
        for mark in current_host().database_marks(transactional):
            test = mark(test)
        setattr(cls, test_name, test)
        cls.__due_work_exemption__ = (source, reason)
        return cls

    return decorate
