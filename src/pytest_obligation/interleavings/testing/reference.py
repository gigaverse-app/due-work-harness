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
) -> Iterator[InFlightSession[int, str, str]]:
    desired: dict[int, str] = {}
    remote: dict[int, str] = {}
    revisions: dict[int, int] = {}
    ack: dict[int, int] = {}
    finished: set[int] = set()
    provider = ProviderControl(accept=lambda apply: AcceptedProviderRequest(apply=apply))

    def write(handle: int) -> None:
        value = desired[handle]
        try:
            provider.invoke("write", str(handle), lambda: remote.__setitem__(handle, value))
            ack[handle] = revisions[handle]
            finished.add(handle)
        except TimeoutError:
            pass

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
            if remote.get(handle) != desired[handle] or ack.get(handle) != revisions[handle]:
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
