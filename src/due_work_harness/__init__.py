"""
Conformance proofs for due work: work a program records now and a worker
completes later.

Due work can be delayed, retried, lost, duplicated or recovered. This package
proves an application's real production code handles each of those histories
without the application writing the proofs. An adopter declares a contract that
binds its real selection, worker and state; the harness generates the proofs.
The adopter never supplies an expected outcome.

Start here:

* :class:`DueWorkContract` and :func:`due_work_contract_suite` — declare a
  disposition (:class:`Claim`, :class:`Decline`, :class:`NotApplicable`,
  :class:`KnownGap`) for every :class:`Profile` and get a pytest suite.
* :class:`Host` and :func:`configure` — what a framework supplies to the
  framework-free proofs; integrations under ``due_work_harness.integrations``
  build hosts for particular frameworks.
* ``ARCHITECTURE.md`` for the layout and host contract, and
  ``docs/what-a-green-result-means.md`` for what a pass does and does not mean.

The core imports no framework. Everything re-exported here loads with only
pytest installed.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("due-work-harness")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"


from due_work_harness.contract import (
    Adoption,
    Claim,
    ContractCase,
    Decline,
    DueWorkContract,
    DueWorkContractDesignError,
    DueWorkSource,
    ExtraProof,
    KnownGap,
    NotApplicable,
    Profile,
    SafetyContract,
    SafetyProfile,
    ScheduledSelection,
    contract_cases,
    contract_report,
    due_work_contract_suite,
    safety_contract_cases,
    safety_contract_suite,
    scheduled_selection_cases,
    scheduled_selection_suite,
)
from due_work_harness.crash_histories import (
    CallableDelivery,
    Delivery,
    ExternalCall,
    HandoffHistory,
    HistoryRun,
    assert_crash_at_every_commit_converges,
    assert_histories_converge,
)
from due_work_harness.gap_probes import (
    DisprovenCapability,
    LossIsAbsorbedElsewhere,
    MissingReclaim,
    MissingScheduledConsumer,
)
from due_work_harness.helpers import (
    assert_provider_call_holds_no_transaction,
    contract_params,
    undeclared,
)
from due_work_harness.host import (
    Host,
    SelectionInspector,
    WorkerKiller,
    configure,
    current_host,
    hosted,
)
from due_work_harness.profiles.automatic_recovery import (
    DUE_WORK_PROOFS,
    SELECTION_PROOFS,
    DueWorkSweep,
    InFlightExecution,
    OwedWorkVariant,
    assert_due_work_recovery_contract,
)
from due_work_harness.profiles.bounded_ownership import (
    FENCED_OWNERSHIP_PROOFS,
    FencedOwnership,
    assert_fenced_ownership_contract,
)
from due_work_harness.profiles.crash_ambiguity import (
    AMBIGUITY_PROOFS,
    AmbiguityAware,
    assert_ambiguity_contract,
)
from due_work_harness.profiles.durable_retention import (
    RETENTION_PROOFS,
    Retention,
    assert_retention_contract,
    assert_retention_preserves_non_terminal_work,
)
from due_work_harness.profiles.eventual_convergence import (
    CONVERGENT_WRITE_PROOFS,
    SNAPSHOT_PROOFS,
    ConvergentWrite,
    SupersededSnapshot,
    assert_convergent_write_contract,
    assert_superseded_snapshot_contract,
    assert_superseded_snapshot_does_not_write,
)
from due_work_harness.profiles.fact_derived_obligations import (
    STATE_DERIVED_PROOFS,
    StateDerived,
    assert_state_derived_contract,
)
from due_work_harness.safety.bounded_retry import (
    BOUNDED_RETRY_PROOFS,
    BoundedRetry,
    assert_bounded_retry_contract,
)
from due_work_harness.safety.replay_safe_execution import (
    REPLAY_SAFETY_PROOFS,
    ReplaySafeEffect,
    assert_replay_safety_contract,
)

__all__ = [
    "__version__",
    # Contract layer.
    "Adoption",
    "Claim",
    "ContractCase",
    "Decline",
    "DueWorkContract",
    "DueWorkContractDesignError",
    "DueWorkSource",
    "ExtraProof",
    "KnownGap",
    "NotApplicable",
    "Profile",
    "SafetyContract",
    "SafetyProfile",
    "ScheduledSelection",
    "contract_cases",
    "contract_report",
    "due_work_contract_suite",
    "safety_contract_cases",
    "safety_contract_suite",
    "scheduled_selection_cases",
    "scheduled_selection_suite",
    # Host.
    "Host",
    "SelectionInspector",
    "WorkerKiller",
    "configure",
    "current_host",
    "hosted",
    # Helpers.
    "assert_provider_call_holds_no_transaction",
    "contract_params",
    "undeclared",
    # Gap probes.
    "DisprovenCapability",
    "LossIsAbsorbedElsewhere",
    "MissingReclaim",
    "MissingScheduledConsumer",
    # Crash histories.
    "CallableDelivery",
    "Delivery",
    "ExternalCall",
    "HandoffHistory",
    "HistoryRun",
    "assert_crash_at_every_commit_converges",
    "assert_histories_converge",
    # Profile A: automatic recovery.
    "DUE_WORK_PROOFS",
    "SELECTION_PROOFS",
    "DueWorkSweep",
    "InFlightExecution",
    "OwedWorkVariant",
    "assert_due_work_recovery_contract",
    # Profile B: bounded ownership.
    "FENCED_OWNERSHIP_PROOFS",
    "FencedOwnership",
    "assert_fenced_ownership_contract",
    # Profile C: crash ambiguity.
    "AMBIGUITY_PROOFS",
    "AmbiguityAware",
    "assert_ambiguity_contract",
    # Profile D: durable retention.
    "RETENTION_PROOFS",
    "Retention",
    "assert_retention_contract",
    "assert_retention_preserves_non_terminal_work",
    # Profile E: eventual convergence.
    "CONVERGENT_WRITE_PROOFS",
    "SNAPSHOT_PROOFS",
    "ConvergentWrite",
    "SupersededSnapshot",
    "assert_convergent_write_contract",
    "assert_superseded_snapshot_contract",
    "assert_superseded_snapshot_does_not_write",
    # Profile F: fact-derived obligations.
    "STATE_DERIVED_PROOFS",
    "StateDerived",
    "assert_state_derived_contract",
    # Execution safety.
    "BOUNDED_RETRY_PROOFS",
    "REPLAY_SAFETY_PROOFS",
    "BoundedRetry",
    "ReplaySafeEffect",
    "assert_bounded_retry_contract",
    "assert_replay_safety_contract",
]
