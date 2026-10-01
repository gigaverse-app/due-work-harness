"""Canonical guarantee names; proof families and execution engines do not allocate letters."""

from enum import Enum


class Profile(Enum):
    """Stable A–J identities, with descriptive aliases for readable declarations."""

    A = "Automatic Recovery"
    B = "Bounded Ownership"
    C = "Crash Ambiguity"
    D = "Durable Retention"
    E = "Eventual Convergence"
    F = "Fact-Derived Obligations"
    G = "Gated Execution"
    H = "Harmless Replay"
    I = "Indivisible Admission"  # noqa: E741 — canonical alphabetic profile identity
    J = "Job Retry Limits"

    AUTOMATIC_RECOVERY = A
    BOUNDED_OWNERSHIP = B
    CRASH_AMBIGUITY = C
    DURABLE_RETENTION = D
    EVENTUAL_CONVERGENCE = E
    FACT_DERIVED_OBLIGATIONS = F
    GATED_EXECUTION = G
    HARMLESS_REPLAY = H
    INDIVISIBLE_ADMISSION = I
    JOB_RETRY_LIMITS = J

    @property
    def title(self) -> str:
        """The enum value is the canonical name used by declarations, errors and reports."""
        return self.value

    @property
    def case_prefix(self) -> str:
        """Preserve existing safety case IDs while giving their guarantees canonical letters."""
        return {Profile.H: "REPLAY_SAFE_EXECUTION", Profile.J: "BOUNDED_RETRY"}.get(self, self.name)


SAFETY_PROFILES = (Profile.H, Profile.J)


class ConvergenceFamily(Enum):
    """Independent E coverage decisions; claiming one never certifies its siblings."""

    STALE_SNAPSHOTS = "snapshot"
    MONOTONIC_RESULTS = "convergence"
    IN_FLIGHT = "in_flight"
    EVIDENCE_CONFLUENCE = "evidence_confluence"
