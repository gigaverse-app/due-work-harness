"""Typed production bindings; the catalog and runner own schedules and assertions."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Annotated, Generic, Self, TypeVar

from pydantic import Field, InstanceOf, StrictInt, model_validator

from pytest_obligation.models import HarnessModel

from .engine.provider import ProviderControl
from .model import Bounds, History, KnownFailure
from .ports import Transport

# Version-1 retry traces encode aliases as comma-separated values. Reject empty
# or ambiguous aliases at declaration time instead of breaking saved replay.
_Alias = Annotated[str, Field(min_length=1, pattern=r"^[^,]+$")]
HandleT = TypeVar("HandleT")
ValueT = TypeVar("ValueT")
ObservationT = TypeVar("ObservationT")


class Intent(HarnessModel, Generic[ValueT, ObservationT]):
    """Canonical command value paired with its reviewed outcome, never learned from the run."""

    value: ValueT = Field(description="Canonical application command input; passed unchanged to admit/change.")
    expected: ObservationT = Field(
        description="Reviewed settled observation for this command; independent of observed execution."
    )


class InFlightSession(HarnessModel, Generic[HandleT, ValueT, ObservationT]):
    """
    Validated wiring for one in-flight history; the binding context owns mutable resources.

    HandleT identifies real admitted work; ValueT is the canonical command input;
    ObservationT is an independently observed product/provider outcome. Callbacks
    are synchronous; ignored return values are typed object. Observers are read-only.
    Freezing protects wiring, not the application objects reached through callbacks.
    """

    intents: Mapping[str, Intent[ValueT, ObservationT]] = Field(
        description="Stable alias to canonical input and reviewed outcome; keys must match the declaration."
    )
    admit: Callable[[ValueT], HandleT] = Field(
        description="Create durable work through production admission from value; return its handle."
    )
    start: Callable[[HandleT], object] = Field(
        description="Run the real worker for handle, or release its captured deliveries; return value is ignored."
    )
    observe: Callable[[HandleT], ObservationT] = Field(
        description="Read product and external-provider state for handle without changing either."
    )
    recover: Callable[[], object] = Field(
        description="Invoke the real recovery entry point; do not synthesize or repair application rows."
    )
    advance: Callable[[int], object] = Field(
        description="Advance the controlled scheduler clock by the supplied number of seconds; no wall-clock sleep."
    )
    retained: Callable[[HandleT], bool] = Field(
        description="Return whether the handle still has a durable recovery owner while provider work is pending."
    )
    provider: InstanceOf[ProviderControl] = Field(
        description="Fresh external-only fault controller, retained by identity without revalidating its live ledger."
    )
    change: Callable[[HandleT, ValueT], object] | None = Field(
        default=None,
        description="Optional production desired-value command (handle, value); must create a new revision identity.",
    )
    retire: Callable[[HandleT], object] | None = Field(
        default=None,
        description="Optional production retirement command for handle; required by retirement declarations.",
    )
    retired: ObservationT | None = Field(
        default=None,
        description="Reviewed product/provider observation after retirement; required when retire is supplied.",
    )
    transport: InstanceOf[Transport] | None = Field(
        default=None, description="Optional external delivery controller; presence must match the declaration."
    )
    account_faults: Callable[[], None] = Field(
        default=lambda: None,
        description=(
            "Account for deliberately injected worker failures after each step/recovery; must not hide "
            "unrelated errors."
        ),
    )
    # Revision identity must be independent of payload equality; optional only for retirement.
    desired_identity: Callable[[HandleT], str] | None = Field(
        default=None,
        description=(
            "Read current commanded revision identity for handle, independent of payload equality (including ABA)."
        ),
    )
    acknowledged_identity: Callable[[HandleT], str | None] | None = Field(
        default=None,
        description="Read the revision acknowledged by production for handle, or None before acknowledgement.",
    )


class EvidenceArrival(HarnessModel):
    """Publish an external fact, then invoke the production consumer of available facts."""

    publish: Callable[[], None] = Field(
        description="Expose a fact only in the external evidence source; never write production outcome rows."
    )
    consume: Callable[[], object] = Field(
        description="Invoke the real evidence consumer. Batchable arrivals must share an equal consumer callable."
    )

    def __call__(self) -> object:
        """Publish one fact and return the production consumer result (ignored by the engine)."""
        self.publish()
        return self.consume()


class EvidenceExpectation(HarnessModel, Generic[ObservationT]):
    """One reviewed outcome and its exact external effects, independent of the observed run."""

    observation: ObservationT = Field(
        description="Reviewed product outcome for a delivered fact set; compare directly with observe()."
    )
    effects: Mapping[str, Annotated[StrictInt, Field(ge=0)]] = Field(
        description=(
            "Exact external effect identity-to-count ledger, including absence of unexpected identities; "
            "counts are strict nonnegative integers."
        )
    )


class EvidenceRetry(HarnessModel, Generic[ObservationT]):
    """Optional real sender turn and the obligations that apply after it."""

    send: Callable[[Callable[[], None]], object] = Field(
        description=(
            "Run the actual sender; invoke the supplied replay callback at the new-attempt boundary "
            "before it settles. Return value is ignored."
        )
    )
    expectations: Mapping[frozenset[str], EvidenceExpectation[ObservationT]] = Field(
        description="Reviewed post-retry outcomes and exact effects for every reachable original fact set."
    )


class EvidenceSession(HarnessModel, Generic[ObservationT]):
    """
    Validated wiring for one evidence history, with reviewed outcomes for every reachable fact set.

    The engine checks capability/metadata agreement and oracle completeness before
    prepare runs. All callbacks are synchronous. Observers must not repair state;
    facts, recovery and retry must reach production transitions. The binding context
    creates and cleans up fresh resources for every fixed or explored history.
    """

    prepare: Callable[[], object] = Field(
        description=(
            "Create uncertain production state through the real workflow, after engine preflight; return "
            "value is ignored."
        )
    )
    facts: Mapping[str, Callable[[], object]] = Field(
        description="Alias to real evidence action, or EvidenceArrival for batching; keys must match the declaration."
    )
    observe: Callable[[], ObservationT] = Field(
        description=(
            "Read current product outcome independently of the expected values; must not mutate application state."
        )
    )
    expectations: Mapping[frozenset[str], EvidenceExpectation[ObservationT]] = Field(
        description="Reviewed pre-retry outcomes for every reachable fact set, including the empty set."
    )
    recover: Callable[[], object] = Field(
        description="Invoke real recovery without fabricating evidence or writing reviewed outcomes directly."
    )
    advance: Callable[[int], object] = Field(
        description="Advance the controlled scheduler clock by the supplied seconds before recovery."
    )
    effects: Callable[[], Mapping[str, int]] = Field(
        description="Read the complete external effect identity-to-count ledger; observation must not consume/reset it."
    )
    # Real connection ownership/cleanup is supplied by the backend adapter.
    actor_scope: Callable[[], AbstractContextManager[object]] = Field(
        description=(
            "Create a fresh context inside each ordered actor thread; own connection, transaction and bounded cleanup."
        )
    )
    retry: EvidenceRetry[ObservationT] | None = Field(
        default=None, description="Optional sender-turn binding; presence must match retry_turnover in the declaration."
    )
    # A stable snapshot of the original attempt after settlement, independent of the live object.
    settled_evidence: Callable[[], str] | None = Field(
        default=None,
        description=(
            "Read a stable serialized snapshot of the ORIGINAL attempt; required for retry, never switch "
            "to the newest attempt."
        ),
    )


class Scenario(HarnessModel, ABC):
    """Pure collection metadata; constructing a declaration never enters its binding."""

    name: str = Field(
        min_length=1, description="Stable scenario alias recorded in replay traces; must match its contract map key."
    )
    bounds: Bounds = Field(
        default_factory=Bounds, description="Clock/recovery budget shared by fixed histories, exploration and replay."
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Binding-semantics version; increment when saved histories no longer mean the same thing.",
    )
    gaps: Mapping[str, KnownFailure] = Field(
        default_factory=dict,
        description="Legacy-only exact history IDs and expected invariant failures; unexpected failures remain red.",
    )

    @abstractmethod
    def histories(self) -> tuple[History, ...]:
        """Collect stable, deduplicated schedules without entering the binding context."""
        ...

    @abstractmethod
    def run(self, history: History) -> None:
        """Execute a complete history in fresh resources; failures carry a replay trace."""
        ...

    @abstractmethod
    def limitations(self) -> Mapping[str, str]:
        """Map omitted history families to explicit domain reasons for coverage reports."""
        ...

    def validate_definition(self) -> None:
        """Reject incomplete capability declarations and gaps outside the generated catalog."""
        assert all(self.limitations().values()), "missing domain reason for an inapplicable family"
        assert set(self.gaps) <= {h.id for h in self.histories()}, "gap names an uncollected history"

    @model_validator(mode="after")
    def check_definition(self) -> Self:
        """Enforce pure collection invariants at construction, before any application side effects."""
        self.validate_definition()
        return self


class InFlightConvergence(Scenario, Generic[HandleT, ValueT, ObservationT]):
    """
    Profile E family generating held/lost/refused provider work against revisions or retirement.

    Declaring capabilities selects root-owned histories and invariant checks;
    adopters supply commands and observations, never their own schedules.
    """

    bind: Callable[[], AbstractContextManager[InFlightSession[HandleT, ValueT, ObservationT]]] = Field(
        description=(
            "Context factory yielding fresh validated wiring; must clean up on failure and every exploration example."
        )
    )
    intents: tuple[_Alias, ...] = Field(
        description="Unique command aliases in canonical transition order; revision histories require at least two."
    )
    seams: tuple[_Alias, ...] = Field(
        description="External provider seam names where held/refused/response-lost faults can be injected."
    )
    repair_seams: tuple[_Alias, ...] = Field(
        default=(),
        description="External repair seam names exercised after retirement; empty when the domain has no repair seam.",
    )
    retirement: bool = Field(
        default=False, description="Generate retirement histories instead of desired-value revision histories."
    )
    independent: bool = Field(
        default=False, description="Generate a second admission to check progress while the first is held."
    )
    transport: bool = Field(
        default=False, description="Generate lost and duplicate notifications; requires a session transport controller."
    )
    replay_safe: bool = Field(
        default=True,
        description="Whether repeating a provider call is allowed; False checks at most one call per seam/identity.",
    )
    # Reasons are required for domain shapes that cannot exercise revision cases.
    limited_revisions_because: str = Field(
        default="",
        description="Domain reason fewer than three revision intents are meaningful; required when applicable.",
    )
    no_independent_because: str = Field(
        default="", description="Domain reason independent-admission histories are inapplicable."
    )
    no_transport_because: str = Field(
        default="", description="Domain reason notification-loss/redelivery histories are inapplicable."
    )
    no_repair_because: str = Field(default="", description="Domain reason retirement has no external repair seam.")

    def histories(self) -> tuple[History, ...]:
        from .engine.catalog import in_flight_histories

        return in_flight_histories(
            self.intents, self.seams, self.retirement, self.independent, self.transport, self.repair_seams
        )

    def limitations(self) -> Mapping[str, str]:
        result = {}
        if self.retirement:
            result["revision histories"] = "Retirement lifecycle has no mutable desired-value command."
        elif len(self.intents) < 3:
            result["three revisions and ABA"] = self.limited_revisions_because
        if not self.independent:
            result["independent progress"] = self.no_independent_because
        if not self.transport:
            result["lost/duplicate notifications"] = self.no_transport_because
        if self.retirement and not self.repair_seams:
            result["repair fails once"] = self.no_repair_because
        return result

    def validate_definition(self) -> None:
        assert self.intents and self.seams and len(set(self.seams)) == len(self.seams)
        assert len(set(self.intents)) == len(self.intents)
        assert self.retirement or len(self.intents) >= 2, "revision bindings require a legal transition"
        super().validate_definition()

    def run(self, history: History) -> None:
        from .engine.runner import run_in_flight

        run_in_flight(self, history)


class EvidenceConfluence(Scenario, Generic[ObservationT]):
    """
    Profile E family generating fact order, duplication, recovery, batching and optional retry histories.

    Every legal ordering must converge to the reviewed outcome for its delivered
    fact set. Ordered actors use separate connections sequentially, not overlapping
    transactions. Dependencies mean prior consumption, not presence in one batch.
    """

    bind: Callable[[], AbstractContextManager[EvidenceSession[ObservationT]]] = Field(
        description="Context factory yielding fresh wiring and independent oracles; must clean up after each history."
    )
    facts: tuple[_Alias, ...] = Field(
        description="Two or three unique evidence aliases; canonical order is the control history."
    )
    dependencies: tuple[tuple[_Alias, _Alias], ...] = Field(
        default=(),
        description="Pairs (prerequisite, dependent); prerequisite must have been consumed in an earlier step.",
    )
    ordered_pair: tuple[_Alias, _Alias] | None = Field(
        default=None,
        description=(
            "Optional pair run in both orders on separate actor threads/connections, with full cleanup between actors."
        ),
    )
    retry_turnover: bool = Field(
        default=False,
        description=(
            "Generate real sender retry turns with replay of old facts; requires session.retry and settled_evidence."
        ),
    )
    batchable: bool = Field(
        default=False,
        description=(
            "Generate ordered arrival batches: publish all facts, then consume once; requires EvidenceArrival bindings."
        ),
    )
    no_ordered_pair_because: str = Field(
        default="", description="Domain reason separate ordered actor connections are inapplicable."
    )
    no_retry_because: str = Field(default="", description="Domain reason sender retry turnover is inapplicable.")

    def histories(self) -> tuple[History, ...]:
        from .engine.catalog import evidence_histories

        return evidence_histories(self.facts, self.dependencies, self.ordered_pair, self.retry_turnover, self.batchable)

    def limitations(self) -> Mapping[str, str]:
        return {
            **({"owner/reconciler connections": self.no_ordered_pair_because} if self.ordered_pair is None else {}),
            **({"retry turnover": self.no_retry_because} if not self.retry_turnover else {}),
        }

    def validate_definition(self) -> None:
        assert 2 <= len(self.facts) <= 3 and len(set(self.facts)) == len(self.facts)
        assert all(a in self.facts and b in self.facts and a != b for a, b in self.dependencies)
        assert self.ordered_pair is None or (
            len(set(self.ordered_pair)) == 2 and set(self.ordered_pair) <= set(self.facts)
        )
        super().validate_definition()

    def run(self, history: History) -> None:
        from .engine.runner import run_evidence

        run_evidence(self, history)
