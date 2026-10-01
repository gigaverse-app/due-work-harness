"""One interpreter for collected histories, exploration and deterministic replay."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import copy_context
from threading import Thread
from typing import TypeVar

from ...binding import INVOCATION_AUTHORING_OPERATIONS, assert_binding_reaches_production
from ...helpers import proof_context
from ..bindings import EvidenceArrival, EvidenceConfluence, EvidenceSession, InFlightConvergence, Scenario
from ..model import Fault, History, HistoryTrace, InterleavingFailure, require
from ..model import Operation as Op
from .causality import reachable_fact_sets

H = TypeVar("H")
V = TypeVar("V")
ObservedT = TypeVar("ObservedT")


def guard(scenario: Scenario, field: str, callback: Callable[..., object]) -> None:
    assert_binding_reaches_production(
        adopter=scenario.name,
        field=field,
        binding=callback,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="delegate to the actual production transition or recovery entry point",
    )


@contextmanager
def traced(scenario: Scenario, history: History) -> Iterator[list[int]]:
    position = [0]
    try:
        yield position
    except InterleavingFailure as error:
        # Record the failing position, but replay the complete history inside a
        # fresh binding. Resuming partial fixture state would hide recovery bugs.
        trace = HistoryTrace(
            scenario=scenario.name,
            scenario_version=scenario.version,
            history=history,
            completed_steps=position[0],
            invariant=error.invariant,
        )
        error.add_note(trace.model_dump_json())
        raise


def replay_history(scenario: Scenario, trace: HistoryTrace) -> None:
    """
    Check binding identity/version, then rerun all stored steps in fresh resources.

    completed_steps is diagnostic only. Replay preserves ordinary failures and
    their new trace notes; it does not apply legacy pytest xfail policy.
    """
    assert trace.scenario == scenario.name and trace.scenario_version == scenario.version, (
        "incompatible replay scenario"
    )
    scenario.run(trace.history)


def run_in_flight(scenario: InFlightConvergence[H, V, ObservedT], history: History) -> None:
    scenario.validate_definition()
    with proof_context(scenario.bind()) as session, traced(scenario, history) as position:
        assert set(session.intents) == set(scenario.intents), "session intents differ from collection metadata"
        assert (session.retire is not None) == scenario.retirement, "retirement seam mismatch"
        assert (session.transport is not None) == scenario.transport, "transport seam mismatch"
        if not scenario.retirement:
            assert session.change and session.desired_identity and session.acknowledged_identity, (
                "revision seams missing"
            )
        guard(scenario, "recover", session.recover)
        if session.change:
            guard(scenario, "change", session.change)
        if session.retire:
            guard(scenario, "retire", session.retire)
        handles: dict[str, H] = {}
        expected: dict[str, ObservedT] = {}
        identities: dict[str, str] = {}

        def recover() -> None:
            session.advance(scenario.bounds.interval_seconds)
            session.recover()
            session.account_faults()

        def check_effects() -> None:
            if not scenario.replay_safe:
                require(
                    all(count <= 1 for (seam, _), count in session.provider.calls.items() if seam in scenario.seams),
                    "non-repeatable",
                    "an uncertain or accepted occurrence was sent again",
                )

        def acknowledged(alias: str) -> bool:
            if session.desired_identity is None or session.acknowledged_identity is None:
                return True
            handle = handles[alias]
            # Equal payloads do not prove the right revision won (A -> B -> A).
            # Pin the commanded identity too: two drifting DB fields must not
            # certify each other during convergence or the quiet tail.
            return session.desired_identity(handle) == identities[alias] == session.acknowledged_identity(handle)

        def is_settled(alias: str) -> bool:
            return session.observe(handles[alias]) == expected[alias] and acknowledged(alias)

        def refuse_confirmed_receipt(alias: str) -> None:
            # A receipt alone cannot confirm an absent effect. A replay-safe
            # writer may already have retried and verified application before
            # START returns; judge that through the independent provider oracle.
            require(
                not acknowledged(alias) or session.observe(handles[alias]) == expected[alias],
                "acknowledged-not-applied",
                f"{alias} is confirmed but the provider only acknowledged receipt and never applied it; "
                "this seam is declared acknowledgement-only, so its reply does not confirm the effect",
            )

        def settle(alias: str) -> None:
            for attempt in range(scenario.bounds.recovery_steps + 1):
                if is_settled(alias):
                    return
                if attempt < scenario.bounds.recovery_steps:
                    recover()
                    check_effects()
            # An acknowledgement-only seam replied without applying, and production
            # still holds that reply as its confirmation after recovery.
            if session.provider.unapplied_acknowledgements and session.observe(handles[alias]) != expected[alias]:
                refuse_confirmed_receipt(alias)
            require(
                False,
                "convergence",
                f"{alias} failed to reach its reviewed product/provider expectation within "
                f"{scenario.bounds.recovery_steps} recovery steps",
            )

        for item in history.steps:
            match item.operation:
                case Op.ARM:
                    assert (
                        item.fault != Fault.ACKNOWLEDGE_WITHOUT_APPLYING
                        or item.seam in scenario.acknowledgement_only_seams
                    ), f"{item.seam} is not declared acknowledgement-only"
                    session.provider.arm(item.seam, item.fault)
                case Op.ADMIT:
                    assert item.target not in handles, "history reused an admission alias"
                    intent = session.intents[item.value]
                    handles[item.target] = session.admit(intent.value)
                    expected[item.target] = intent.expected
                    if session.desired_identity:
                        identities[item.target] = session.desired_identity(handles[item.target])
                case Op.START:
                    session.start(handles[item.target])
                case Op.CHANGE:
                    assert session.change and session.desired_identity
                    intent = session.intents[item.value]
                    handle = handles[item.target]
                    session.change(handle, intent.value)
                    identity = session.desired_identity(handle)
                    require(identity != identities[item.target], "revision", "a new intent reused the old revision")
                    identities[item.target] = identity
                    expected[item.target] = intent.expected
                case Op.RETIRE:
                    assert session.retire and session.retired is not None
                    session.retire(handles[item.target])
                    expected[item.target] = session.retired
                    if session.desired_identity:
                        identities[item.target] = session.desired_identity(handles[item.target])
                case Op.COMPLETE:
                    session.provider.complete(item.index)
                case Op.RECOVER:
                    recover()
                case Op.SETTLE:
                    settle(item.target)
                case Op.OWED:
                    require(
                        session.retained(handles[item.target]),
                        "retention",
                        "pending provider work lost its recovery owner",
                    )
                case Op.QUIET:
                    before = dict(session.provider.calls)
                    for _ in range(2):
                        recover()
                        for alias in handles:
                            require(
                                is_settled(alias),
                                "settled-state",
                                "settled product/provider state or revision acknowledgement regressed",
                            )
                    require(
                        dict(session.provider.calls) == before,
                        "settled-churn",
                        "recovery repeated settled provider work",
                    )
                case Op.UNCONFIRMED:
                    assert session.desired_identity and session.acknowledged_identity, "revision seams missing"
                    assert session.provider.unapplied_acknowledgements, "history never reached an unapplied receipt"
                    # Checked before recovery can repair a premature confirmation;
                    # already-applied, independently verified work may be confirmed.
                    # Unresolved, ambiguous or evidence-derived dispositions conform.
                    refuse_confirmed_receipt(item.target)
                case Op.DROP:
                    assert session.transport
                    session.transport.drop(item.value == "on")
                case Op.DELIVER:
                    assert session.transport
                    session.transport.redeliver(item.index)
                case _:
                    raise AssertionError(f"invalid in-flight operation: {item.operation}")
            session.account_faults()
            check_effects()
            position[0] += 1
        session.provider.assert_reached()
        assert all(request.completed for request in session.provider.pending), (
            "history leaked a pending provider operation"
        )


def ordered_actors(
    first: Callable[[], object], second: Callable[[], object], scope: Callable[[], AbstractContextManager[object]]
) -> None:
    """Commit each actor on its own thread/connection before starting the next."""
    errors: list[BaseException] = []

    def actor(call: Callable[[], object]) -> None:
        try:
            with scope():
                call()
        except BaseException as error:
            errors.append(error)

    for call in (first, second):
        # Each actor inherits the caller's tenant/context, never its predecessor's
        # mutations. Joining after scope exit makes commit/cleanup part of the
        # ordering boundary, and a failed first actor cannot release the second.
        thread = Thread(target=copy_context().run, args=(actor, call))
        thread.start()
        thread.join(15)
        # A join deadline detects a stuck actor; it cannot cancel arbitrary Python.
        # The backend scope bounds SQL and the process watchdog owns hard hangs.
        assert not thread.is_alive(), "evidence actor failed bounded cleanup"
        if errors:
            raise errors[0]


def validate_evidence_session(scenario: EvidenceConfluence[ObservedT], session: EvidenceSession[ObservedT]) -> None:
    assert set(session.facts) == set(scenario.facts), "evidence metadata and runtime routes differ"
    # Validate ALL reachable obligations before preparing production state. An
    # early known behavioral failure must not conceal a missing later oracle.
    required = reachable_fact_sets(scenario.facts, scenario.dependencies)
    expectations = [session.expectations]
    assert (session.retry is not None) == scenario.retry_turnover, "evidence retry metadata mismatch"
    if session.retry is not None:
        assert session.settled_evidence, "retry needs original-attempt evidence observation"
        expectations.append(session.retry.expectations)
    for outcomes in expectations:
        missing = required - outcomes.keys()
        assert not missing, f"missing independent evidence expectation for {sorted(sorted(key) for key in missing)}"
    if session.retry is not None:
        guard(scenario, "retry", session.retry.send)
    guard(scenario, "recover", session.recover)
    for name, fact in session.facts.items():
        assert isinstance(fact, EvidenceArrival) == scenario.batchable, "evidence batching metadata mismatch"
        guard(scenario, name, fact.consume if isinstance(fact, EvidenceArrival) else fact)
    if scenario.batchable:
        consumers = [fact.consume for fact in session.facts.values() if isinstance(fact, EvidenceArrival)]
        assert all(consumer == consumers[0] for consumer in consumers), "batched facts need one production consumer"


def run_evidence(scenario: EvidenceConfluence[ObservedT], history: History) -> None:
    scenario.validate_definition()
    with proof_context(scenario.bind()) as session, traced(scenario, history) as position:
        validate_evidence_session(scenario, session)
        seen: set[str] = set()
        retried = False
        settled_evidence: str | None = None

        def check() -> None:
            nonlocal settled_evidence
            key = frozenset(seen)
            expectations = session.expectations
            if retried:
                assert session.retry is not None
                expectations = session.retry.expectations
            assert key in expectations, f"missing independent evidence expectation for {sorted(key)}"
            expected = expectations[key]
            require(
                session.observe() == expected.observation,
                "evidence",
                "evidence changed the reviewed recipient/attempt outcome",
            )
            require(
                dict(session.effects()) == dict(expected.effects),
                "effects",
                "provider effect identities or counts differ",
            )
            if len(seen) == len(scenario.facts) and session.settled_evidence is not None:
                snapshot = session.settled_evidence()
                if settled_evidence is None:
                    settled_evidence = snapshot
                require(snapshot == settled_evidence, "immutable-evidence", "settled attempt evidence was rewritten")

        for item in history.steps:
            match item.operation:
                case Op.PREPARE:
                    session.prepare()
                case Op.EVIDENCE:
                    session.facts[item.value]()
                    seen.add(item.value)
                case Op.BATCH:
                    assert scenario.batchable and item.facts, "batch requires staged external arrivals"
                    # Publish the whole batch before ONE production consumption.
                    # Consuming per fact would erase the difference that exposes
                    # the campaign's stranded incremental-receipt defect.
                    consumer: Callable[[], object] | None = None
                    for name in item.facts:
                        arrival = session.facts[name]
                        assert isinstance(arrival, EvidenceArrival)
                        arrival.publish()
                        seen.add(name)
                        consumer = arrival.consume
                    assert consumer is not None
                    consumer()
                case Op.CHECK_EVIDENCE:
                    check()
                case Op.RECOVER:
                    session.advance(scenario.bounds.interval_seconds)
                    session.recover()
                case Op.RACE:
                    ordered_actors(session.facts[item.value], session.facts[item.target], session.actor_scope)
                    seen.update((item.value, item.target))
                case Op.RETRY:
                    assert session.retry

                    def replay(aliases: str = item.value) -> None:
                        if aliases:
                            for name in aliases.split(","):
                                session.facts[name]()

                    assert session.settled_evidence
                    # The sender may create a new attempt; it must never rewrite
                    # the original evidence, including during old-fact replay.
                    original = session.settled_evidence()
                    session.retry.send(replay)
                    require(
                        session.settled_evidence() == original,
                        "immutable-evidence",
                        "retry or late replay rewrote the original attempt",
                    )
                    retried = True
                case _:
                    raise AssertionError(f"invalid evidence operation: {item.operation}")
            position[0] += 1
