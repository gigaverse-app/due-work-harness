"""One set of production bindings reused by the example and real queue integration tests.

The runner changes how the worker is invoked. Commands, SQL, faults, expected
results and all harness-owned assertions stay identical across executors.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from functools import partial
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from adopter_app.catalog import Catalog

from due_work_harness import (
    AdmissionAtomicity,
    AdmissionPublication,
    Claim,
    Decline,
    DueWorkContract,
    ExecutionGate,
    NotApplicable,
    Profile,
    ReplaySafeEffect,
)
from due_work_harness.host import Host, hosted
from due_work_harness.interleavings import (
    EvidenceArrival,
    EvidenceConfluence,
    EvidenceExpectation,
    EvidenceSession,
    InFlightConvergence,
    InFlightSession,
    Intent,
    ProviderControl,
)
from due_work_harness.profiles.catalog import ConvergenceFamily

# Executor implementations are external to the application and may queue this callable.
type Runner = Callable[[Callable[[], None]], None]


def inline(operation: Callable[[], None]) -> None:
    operation()


class InterruptedAdmission(RuntimeError):
    """Injected after the obligation INSERT succeeds, while product intent is also visible."""


class ObservedConnection(sqlite3.Connection):
    """Instrument the real SQL boundary; never begin, commit or rollback for the application."""

    checkpoint: Callable[[], None] | None = None

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        result = super().execute(sql, parameters)
        if self.checkpoint is not None and sql.startswith("INSERT INTO obligations"):
            self.checkpoint()
            raise InterruptedAdmission()
        return result


@contextmanager
def application() -> Iterator[tuple[Catalog, ProviderControl, dict[int, str], set[str], list[AdmissionPublication]]]:
    """Fresh file-backed SQL and external systems for every history, including Hypothesis shrinking."""
    provider = ProviderControl()
    remote: dict[int, str] = {}
    facts: set[str] = set()
    events: list[AdmissionPublication] = []
    with TemporaryDirectory() as directory:
        catalog = Catalog(
            Path(directory) / "catalog.sqlite",
            write=lambda identity, value: provider.invoke(
                "publish", str(identity), lambda: remote.__setitem__(identity, value)
            ),
            read=lambda identity: remote.get(identity, ""),
            notify=lambda identity: events.append(AdmissionPublication(in_transaction=catalog.db.in_transaction)),
            receipts=lambda: facts,
            connection_factory=ObservedConnection,
        )
        try:
            with hosted(
                Host(production_packages=frozenset({"adopter_app"}), in_transaction=lambda: catalog.db.in_transaction)
            ):
                yield catalog, provider, remote, facts, events
        finally:
            catalog.db.close()


class Notifications:
    """External queue control retaining exact published identities for duplicate delivery."""

    def __init__(self, execute: Callable[[int], None]) -> None:
        self.execute = execute
        self.messages: list[int] = []
        self.losing = False

    def publish(self, identity: int) -> None:
        if not self.losing:
            self.messages.append(identity)

    def drop(self, enabled: bool) -> None:
        self.losing = enabled
        if enabled:
            self.messages.clear()

    def redeliver(self, index: int) -> None:
        self.execute(self.messages[index])


@contextmanager
def in_flight(run: Runner = inline) -> Iterator[InFlightSession[int, str, str]]:
    # ARRANGE: a fresh durable catalog; every history gets its own SQL file.
    # REAL PRODUCTION: Catalog admission, revision commands, worker and reconciliation.
    # EXTERNAL SEAM: remote catalog writes accepted, refused or completed late by ProviderControl.
    # OBSERVE: remote values and independently read commanded/acknowledged SQL revision IDs.
    with application() as (catalog, provider, remote, _facts, _events):
        transport = Notifications(lambda identity: run(partial(catalog.execute, identity)))
        catalog.notify = transport.publish
        yield InFlightSession(
            intents={name: Intent(value=name, expected=name) for name in ("red", "green", "blue")},
            admit=catalog.admit,
            change=catalog.change,
            start=lambda identity: run(partial(catalog.execute, identity)),
            recover=lambda: run(catalog.recover),
            advance=lambda seconds: None,
            observe=lambda identity: remote.get(identity, ""),
            retained=lambda identity: identity in catalog.owed(),
            provider=provider,
            transport=transport,
            desired_identity=lambda identity: str(catalog.reserved(identity)[2]),
            acknowledged_identity=lambda identity: str(catalog.acknowledged(identity)),
        )


@contextmanager
def evidence(run: Runner = inline) -> Iterator[EvidenceSession[frozenset[str]]]:
    # ARRANGE: independent delivery receipts for three recipients.
    # REAL PRODUCTION: Catalog.ingest_receipts merges durable receipt identities.
    # EXTERNAL SEAM: the provider's visible receipt list, including partial batches and duplicates.
    # OBSERVE: committed SQL receipts, compared to independently enumerated expected sets.
    with application() as (catalog, _provider, _remote, facts, _events):

        def consume() -> None:
            run(catalog.ingest_receipts)

        names = ("alice", "bob", "carol")
        groups = [frozenset(group) for size in range(4) for group in combinations(names, size)]
        yield EvidenceSession(
            prepare=lambda: None,
            facts={name: EvidenceArrival(publish=partial(facts.add, name), consume=consume) for name in names},
            observe=catalog.received,
            expectations={group: EvidenceExpectation(observation=group, effects={}) for group in groups},
            recover=consume,
            advance=lambda seconds: None,
            effects=lambda: {},
            actor_scope=nullcontext,
        )


@contextmanager
def admission() -> Iterator[AdmissionAtomicity[list[str]]]:
    # ARRANGE: an empty catalog, with no transaction supplied by this binding.
    # REAL PRODUCTION: Catalog.admit owns the product/obligation transaction.
    # EXTERNAL SEAM: notification recorder; interruption after the real SQL obligation INSERT.
    # OBSERVE: independent product rows, obligation IDs and external provider calls.
    with application() as (catalog, provider, _remote, _facts, events):

        @contextmanager
        def during(checkpoint: Callable[[], None]) -> Iterator[None]:
            assert isinstance(catalog.db, ObservedConnection)
            catalog.db.checkpoint = checkpoint
            try:
                yield
            finally:
                catalog.db.checkpoint = None

        yield AdmissionAtomicity(
            name="catalog publication",
            admit=lambda: [catalog.admit("red")],
            observe=lambda: [row[0] for row in catalog.db.execute("SELECT desired FROM products")],
            expected=["red"],
            obligations=catalog.owed,
            outstanding=catalog.eligible,
            effects=lambda: sum(provider.calls.values()),
            publications=lambda: nullcontext(events),
            during=during,
            expected_error=InterruptedAdmission,
        )


@contextmanager
def gate(run: Runner = inline) -> Iterator[ExecutionGate]:
    # ARRANGE: already owed publication waiting for product approval, not for a clock.
    # REAL PRODUCTION: Catalog.approve, eligible/owed selections and the real worker/recovery.
    # EXTERNAL SEAM: notifications are recorded but never delivered, so recovery must find release.
    # OBSERVE: SQL revisions and mutation counter plus independent remote provider state/calls.
    with application() as (catalog, provider, remote, _facts, _events):
        identity = catalog.admit("red", approved=False)

        def calls() -> int:
            return sum(provider.calls.values())

        yield ExecutionGate(
            name="editor approval",
            identity=identity,
            due_work=catalog.eligible,
            owed_work=catalog.owed,
            reserved_state=partial(catalog.reserved, identity),
            routes={"worker": lambda: run(partial(catalog.execute, identity))},
            make_eligible=partial(catalog.approve, identity),
            recover=lambda: run(catalog.recover),
            advance=lambda elapsed: None,
            executions=calls,
            mutation_count=lambda: catalog.db.total_changes,
            observe=lambda: remote.get(identity, ""),
            expected="red",
            effect_calls=calls,
            recovery_interval=timedelta(seconds=1),
            recovery_timeout=timedelta(seconds=5),
        )


@contextmanager
def replay(run: Runner = inline) -> Iterator[ReplaySafeEffect]:
    # ARRANGE: one admitted catalog publication.
    # REAL PRODUCTION: Catalog.execute, deliberately invoked twice for the same identity.
    # EXTERNAL SEAM: remote assignment and its independent call counter.
    # OBSERVE: final remote value; repeated assignment must reach the provider twice.
    with application() as (catalog, provider, remote, _facts, _events):
        yield ReplaySafeEffect(
            name="catalog assignment",
            prepare=lambda: catalog.admit("red"),
            execute=lambda identity: run(partial(catalog.execute, identity)),
            observe=lambda identity: remote.get(identity, ""),
            execution_count_for=lambda identity: provider.calls[("publish", str(identity))],
        )


def catalog_contract(name: str, run: Runner = inline) -> DueWorkContract:
    """The same application guarantees through the chosen worker executor."""
    return DueWorkContract(
        name=name,
        profiles={
            Profile.A: Decline("This small example exposes reconcile explicitly; it ships no periodic scheduler."),
            Profile.B: NotApplicable("A single serial executor runs the example; no lease changes hands."),
            Profile.C: Decline("Remote assignments are repeatable; reconciliation compares actual remote values."),
            Profile.D: NotApplicable("The example has no pruning command; durable owners are retained."),
            Profile.E: Claim(),
            Profile.F: NotApplicable(
                "Admission creates explicit obligation rows; recovery does not derive missing rows."
            ),
            Profile.G: Claim(),
            Profile.H: Claim(),
            Profile.I: Claim(),
            Profile.J: NotApplicable("Reconciliation has no retry limit; it retains intent until convergence."),
        },
        eligibility=partial(gate, run),
        replay=partial(replay, run),
        admission={"publish": admission},
        in_flight={
            "catalog revisions": InFlightConvergence(
                name="catalog revisions",
                bind=partial(in_flight, run),
                intents=("red", "green", "blue"),
                seams=("publish",),
                independent=True,
                transport=True,
            )
        },
        evidence_confluence={
            "delivery receipts": EvidenceConfluence(
                name="delivery receipts",
                bind=partial(evidence, run),
                facts=("alice", "bob", "carol"),
                batchable=True,
                no_ordered_pair_because="One serial executor owns this example's SQLite connection.",
                no_retry_because="Receipt ingestion has no sender attempt turnover; recipient facts are immutable.",
            )
        },
        convergence_families={
            ConvergenceFamily.STALE_SNAPSHOTS: NotApplicable(
                "No separate snapshot merge API; in-flight revisions exercise the worker."
            ),
            ConvergenceFamily.MONOTONIC_RESULTS: NotApplicable(
                "The product accepts reversible colors; receipts use evidence confluence."
            ),
        },
    )
