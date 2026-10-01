"""Root-owned reference for testing the runner; never an application conformance claim."""

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from functools import partial
from itertools import combinations
from typing import Literal

from pytest_obligation.interleavings.engine.provider import AcceptedProviderRequest

from ..bindings import EvidenceArrival, EvidenceExpectation, EvidenceSession, InFlightSession, Intent
from ..engine.provider import ProviderControl


@contextmanager
def reference(
    *,
    forget: bool = False,
    forget_at: int = 0,
    quiet_corruption: Literal["acknowledgement", "desired-revision"] | None = None,
    replay_safe: bool = True,
    receipt_only: bool = False,
) -> Iterator[InFlightSession[int, str, str]]:
    """
    Conforming writer unless a counterfeit flag is set.

    By default the writer confirms from the provider's reply, which is correct for
    an API that guarantees completion. receipt_only confirms only from the
    provider's applied revision, as an acknowledgement-only seam requires.
    forget skips handles believed finished during recovery. replay_safe=False
    sends each revision at most once.
    """
    desired: dict[int, str] = {}
    remote: dict[int, str] = {}
    applied: dict[int, int] = {}
    revisions: dict[int, int] = {}
    ack: dict[int, int] = {}
    finished: set[int] = set()
    sent: set[str] = set()
    provider = ProviderControl(accept=lambda apply: AcceptedProviderRequest(apply=apply))

    def apply(handle: int, value: str, revision: int) -> None:
        remote[handle] = value
        applied[handle] = revision

    def reconcile(handle: int) -> None:
        # Provider state, not the reply, is the evidence of an applied revision.
        if applied.get(handle) == revisions[handle]:
            ack[handle] = revisions[handle]

    def write(handle: int) -> None:
        value, revision = desired[handle], revisions[handle]
        identity = str(handle) if replay_safe else f"{handle}@{revision}"
        if not replay_safe and identity in sent:
            return  # An accepted or uncertain occurrence is never sent again.
        sent.add(identity)
        try:
            provider.invoke("write", identity, partial(apply, handle, value, revision))
        except TimeoutError:
            return
        finished.add(handle)
        if receipt_only:
            reconcile(handle)
        else:
            ack[handle] = revision  # The reply confirms completion.

    def admit(value: str) -> int:
        handle = len(desired)
        desired[handle] = value
        revisions[handle] = 1
        return handle

    def change(handle: int, value: str) -> None:
        desired[handle] = value
        revisions[handle] += 1
        write(handle)

    def recover() -> None:
        for handle in desired:
            if (forget or (forget_at and revisions[handle] >= forget_at)) and handle in finished:
                continue
            if not replay_safe:
                reconcile(handle)
                if ack.get(handle) != revisions[handle]:
                    write(handle)
            elif remote.get(handle) != desired[handle] or ack.get(handle) != revisions[handle]:
                write(handle)
        if quiet_corruption:
            for handle in finished:
                ack[handle] = revisions[handle] + 1
                if quiet_corruption == "desired-revision":
                    revisions[handle] = ack[handle]

    yield InFlightSession(
        intents={value: Intent(value=value, expected=value) for value in ("A", "B", "C")},
        admit=admit,
        start=write,
        change=change,
        observe=lambda h: remote.get(h, ""),
        recover=recover,
        advance=lambda seconds: None,
        retained=lambda h: True,
        provider=provider,
        desired_identity=lambda h: str(revisions[h]),
        acknowledged_identity=lambda h: str(ack.get(h)),
    )


@contextmanager
def evidence_reference(*, forget: bool = False) -> Iterator[EvidenceSession[frozenset[str]]]:
    visible: set[str] = set()
    applied: set[str] = set()
    closed = False

    def consume() -> None:
        nonlocal closed
        if not closed:
            applied.update(visible)
        if forget and visible:
            closed = True

    names = ("x", "y", "z")
    expected = {frozenset(group): frozenset(group) for size in range(4) for group in combinations(names, size)}
    yield EvidenceSession(
        prepare=lambda: None,
        facts={name: EvidenceArrival(publish=partial(visible.add, name), consume=consume) for name in names},
        observe=lambda: frozenset(applied),
        expectations={
            group: EvidenceExpectation(observation=observation, effects={}) for group, observation in expected.items()
        },
        recover=consume,
        advance=lambda seconds: None,
        effects=lambda: {},
        actor_scope=nullcontext,
    )
