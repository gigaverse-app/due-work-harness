"""Portable, versioned history format. No application/provider data is serialized."""

from enum import StrEnum

from pydantic import Field

from pytest_obligation.models import HarnessModel


class Operation(StrEnum):
    """Engine instructions shared by fixed catalogs, exploration and replay; Step documents operand encoding."""

    ARM = "arm"
    ADMIT = "admit"
    START = "start"
    CHANGE = "change"
    RETIRE = "retire"
    COMPLETE = "complete"
    RECOVER = "recover"
    SETTLE = "settle"
    OWED = "owed"
    QUIET = "quiet"
    DROP = "drop"
    DELIVER = "deliver"
    EVIDENCE = "evidence"
    BATCH = "batch"
    CHECK_EVIDENCE = "check_evidence"
    PREPARE = "prepare"
    RACE = "race"
    RETRY = "retry"
    UNCONFIRMED = "unconfirmed"


class Fault(StrEnum):
    """
    External boundary outcomes injected by the provider controller.

    HOLD accepts for later completion; LOSE_RESPONSE applies then raises;
    REFUSE raises before applying; ACKNOWLEDGE_WITHOUT_APPLYING returns an
    ordinary reply once without applying, for seams declared acknowledgement-only.
    """

    HOLD = "hold"
    LOSE_RESPONSE = "lose_response"
    REFUSE = "refuse"
    ACKNOWLEDGE_WITHOUT_APPLYING = "acknowledge_without_applying"


class Step(HarnessModel):
    """
    One portable instruction containing aliases only, never application IDs or provider data.

    Operand meanings are operation-specific; unused fields retain their wire defaults.
    RACE is the historical wire name for sequential actors on separate connections.
    It does not claim simultaneous execution.
    """

    operation: Operation = Field(description="Instruction interpreted by the selected profile.")
    target: str = Field(default="a", description="Handle alias for work operations; second fact alias for RACE.")
    value: str = Field(
        default="",
        description=(
            "Intent alias for ADMIT/CHANGE; fact for EVIDENCE; first fact for RACE; on/off for DROP; "
            "comma-separated old facts for RETRY."
        ),
    )
    seam: str = Field(default="", description="External provider seam to arm for ARM.")
    fault: Fault = Field(default=Fault.HOLD, description="Fault to inject at the next matching seam for ARM.")
    index: int = Field(
        default=0,
        ge=0,
        description="Zero-based accepted request for COMPLETE, or captured worker delivery for DELIVER.",
    )
    facts: tuple[str, ...] = Field(
        default=(), description="Ordered external fact aliases published together by BATCH before one consumer call."
    )


class History(HarnessModel):
    """An ordered schedule with a stable catalog ID and all invariant families it exercises."""

    id: str = Field(description="Stable catalog identity used by reports and exact known-gap declarations.")
    families: tuple[str, ...] = Field(
        description="All covered schedule families; equivalent step sequences retain their combined coverage labels."
    )
    steps: tuple[Step, ...] = Field(
        description="Complete sequence interpreted in order, including explicit invariant checkpoints."
    )


class HistoryTrace(HarnessModel):
    """Versioned failure artifact replayed from the beginning inside a fresh binding context."""

    version: int = Field(
        default=1, ge=1, le=1, description="Trace wire-format version; only version 1 is currently readable."
    )
    catalog_version: int = Field(
        default=2,
        ge=1,
        le=2,
        description="Catalog format generation; versions 1 and 2 replay their stored steps directly.",
    )
    scenario: str = Field(description="Stable binding name; must match the target declaration.")
    scenario_version: int = Field(
        ge=1, description="Semantic binding version; mismatches are rejected before execution."
    )
    history: History = Field(description="Full schedule to replay, including steps after the original failure.")
    completed_steps: int = Field(
        default=0, ge=0, description="Diagnostic count of steps completed before failure; never a resume offset."
    )
    invariant: str | None = Field(
        default=None, description="Failed invariant identifier, or None for a trace without a recorded failure."
    )


class Bounds(HarnessModel):
    """Deterministic simulated-time recovery budget; this is not a wall-clock test timeout."""

    interval_seconds: int = Field(default=60, gt=0, description="Controlled clock advance before each recovery turn.")
    recovery_steps: int = Field(
        default=30,
        gt=0,
        le=1000,
        description="Maximum recovery turns allowed while settling; observation is checked once before recovery.",
    )


class KnownFailure(HarnessModel):
    """Reviewed legacy debt scoped to one history ID by Scenario.gaps and one invariant here."""

    invariant: str = Field(
        min_length=1, description="Exact failure identifier eligible for strict legacy xfail; other failures stay red."
    )
    reason: str = Field(
        min_length=20,
        description="Concrete explanation of the unresolved production defect, surfaced in pytest reports.",
    )


class InterleavingFailure(AssertionError):
    """Behavioral invariant violation; authoring/validation errors use separate exception types."""

    def __init__(self, invariant: str, message: str) -> None:
        self.invariant = invariant
        super().__init__(f"{invariant}: {message}")


class KnownInterleavingFailure(InterleavingFailure):
    """Only the explicitly declared behavioral failure may satisfy a legacy xfail."""


def require(condition: bool, invariant: str, message: str) -> None:
    """Raise a named behavioral failure suitable for trace recording and exact legacy matching."""
    if not condition:
        raise InterleavingFailure(invariant, message)
