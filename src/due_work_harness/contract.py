"""
The declarative binding surface of the due-work harness.

The profile modules under :mod:`due_work_harness.profiles`
(:mod:`~due_work_harness.profiles.automatic_recovery`,
:mod:`~due_work_harness.profiles.bounded_ownership`,
:mod:`~due_work_harness.profiles.crash_ambiguity`,
:mod:`~due_work_harness.profiles.durable_retention`,
:mod:`~due_work_harness.profiles.eventual_convergence`,
:mod:`~due_work_harness.profiles.fact_derived_obligations`) own the invariants.
This module owns how a domain *binds* to them, and it exists to close the gap
the first adoptions exposed: every adopter re-invented its own parametrization,
its own xfail bookkeeping, its own schedule inspection, and its own choice of
which proofs to apply — hundreds of lines of bespoke test code per domain, and
no way to see which profiles a domain never accounted for.

The position is: **mechanism-neutral is not structure-neutral.** A domain keeps
its own tables, states, workers and scheduler — nothing here requires a shared
model, queue, lease or runtime. What every domain must expose is the same small
*semantic* surface: a callable selection, a callable tick, explicit terminal
examples, explicit bounds and scheduling, and a disposition for every profile.
Once that surface exists, invariant selection, parametrization, naming, xfail
discipline and assertions all come from here, mechanically.

Nothing here imports a framework. Where a generated case needs something only
the application's framework can supply — the pytest marks that give a test a
database, with or without real commits — it asks the configured
:class:`~due_work_harness.host.Host` when the cases are generated.

What a domain writes
--------------------

One :class:`DueWorkContract` — production bindings, example constructors, and a
disposition for **every** profile — then one decorated class::

    PREVIEWS = DueWorkContract(
        name="document previews",
        adoption=Adoption.LEGACY,  # required by the KnownGap below; see Test policy
        profiles={
            Profile.AUTOMATIC_RECOVERY: KnownGap("nothing scheduled consumes the derived obligation"),
            Profile.BOUNDED_OWNERSHIP: Decline("execution is idempotent; no exclusive ownership"),
            Profile.CRASH_AMBIGUITY: Decline("the storage write is safely repeatable"),
            Profile.DURABLE_RETENTION: NotApplicable("no retention pass exists"),
            Profile.EVENTUAL_CONVERGENCE: Claim(),
            Profile.FACT_DERIVED_OBLIGATIONS: Claim(),
            Profile.GATED_EXECUTION: NotApplicable("No execution prerequisite exists."),
            Profile.HARMLESS_REPLAY: Claim(),
            Profile.INDIVISIBLE_ADMISSION: NotAssessed(because="Partial admission needs assessment."),
            Profile.JOB_RETRY_LIMITS: NotApplicable("The task queue owns transport retries."),
        },
        replay=_replay_binding,
        snapshot=_snapshot_binding,      # profile E, worker half
        derivation=_derivation_binding,  # profile F
    )

    @due_work_contract_suite(
        PREVIEWS,
        covers=(DueWorkSource(DocumentService.upload),),
    )
    class TestDocumentPreviewsDueWork:
        pass

The suite generates one test per applicable invariant
(``TestX::test_due_work_contract[F-assert_stopped_work_is_not_revived]``), one
visible row per declined or not-applicable profile, and one strict xfail per
known gap — so the test report *is* the profile report, and a profile nobody
accounted for is a collection-time design error rather than silence.

Every domain-authored binding factory is also required to place four review
annotations beside the code they justify: ``# ARRANGE``,
``# REAL PRODUCTION``, ``# EXTERNAL SEAM``, and ``# OBSERVE``. Construction
fails when one is absent from the factory's source. A detached prose summary
cannot hide which exact callback is production, what was replaced, or which
line proves the production call ran.

Dispositions — silence is not one of them
-----------------------------------------

Every profile must be one of:

* :class:`NotAssessed` — unfinished assessment, a strict XFAIL with a remediation
  reason. It cannot carry a binding or count as executed behavioral evidence.

* :class:`Claim` — the domain has this capability; its binding is required and
  every proof runs. ``gaps`` records per-invariant **legacy** defects as strict
  xfails, centrally generated.
* :class:`Decline` — the capability is deliberately absent for an architectural
  reason (an edge-triggered sender declining state-derivation because "already
  sent" is not visible in the product state that asked for the send). A decline
  may carry ``prove``, an executable demonstration that the discriminating
  invariant really fails.
* :class:`NotApplicable` — the capability's precondition does not exist in the
  domain at all (no retention pass, so nothing to measure).
* :class:`KnownGap` — the domain *should* have this capability and lacks it
  entirely; a legacy finding awaiting a fix. Generates one strict xfail.
  ``detect`` (optional but better) is a probe that fails while the gap exists,
  so the day the capability lands the xfail trips and forces the declaration
  to flip to :class:`Claim`.

The distinction between the last three is load-bearing: a decline must describe
genuine non-applicability, never missing work — missing work is a
:class:`KnownGap` and stays red-ish until fixed.

Test policy — encoded, not prose
--------------------------------

The policy is a field, and the strict default is the point:

* ``adoption=Adoption.NEW_FEATURE`` (the default) **forbids gap declarations**
  — a ``Claim`` with ``gaps``, a :class:`KnownGap` disposition, an
  :class:`ExtraProof` with ``gap``, or a ``handoff_gaps`` entry is a design
  error. A new due-work feature must pass every claimed profile in the change
  that introduces it; shipping one with known contract gaps would turn the
  harness documentary instead of preventative.
* ``adoption=Adoption.LEGACY`` — adopting code that predates its contract —
  permits them: they record pre-existing defects separately from their fixes,
  and ``strict=True`` forces whoever fixes one to delete its declaration.

Any waiver of the new-feature policy is therefore visible in the diff as an
explicit ``Adoption.LEGACY`` on a new domain, which is exactly the thing a
reviewer can refuse. A fix deletes only the gap declarations it earns; the
evidence that a fix works is that the *same centrally generated test* flips
from xfail to pass.

Bespoke assertion logic does not belong inside a declaration. ``detect``,
``prove``, and ``ExtraProof.run`` bindings must either be root-owned probes
(:mod:`.gap_probes`, or any harness-defined callable) or delegate to a shared
``assert_*`` proof — validated (heuristically, on the binding's code) at
construction. A recurring gap shape belongs in :mod:`.gap_probes`, where it
gets one owner and self-tests, not re-hand-rolled per adopter.

The light contract
------------------

Not every scheduled query is a due-work sweep. :class:`ScheduledSelection`
binds a bare scheduled selection to the selection-only proofs (authorship,
index-served, replica routing) without fabricating a sweep around it — the
honest shape for an index audit of a query that claims and runs work inline.
"""

import inspect
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from enum import Enum
from functools import wraps
from typing import Any

import pytest
from pydantic import Field, InstanceOf, SkipValidation

from due_work_harness.binding import (
    assert_test_binding_delegates_to_production,
    authored_inversion_names,
    callable_code,
    is_harness_owned,
    is_test_authored,
    is_test_code,
    references_harness_assertion,
)
from due_work_harness.coherence import (
    assert_automatic_recovery_consumes_derived_obligations,
)
from due_work_harness.crash_histories import (
    Delivery,
    HandoffHistory,
    HistoriesDiverged,
    assert_crash_at_every_commit_converges,
)
from due_work_harness.helpers import proof_context
from due_work_harness.host import current_host
from due_work_harness.interleavings.adapters import integration as interleaving_integration
from due_work_harness.interleavings.bindings import EvidenceConfluence, InFlightConvergence
from due_work_harness.models import MISSING, DueWorkContractDesignError, HarnessModel, with_positional
from due_work_harness.process_histories import ProcessHistory, assert_process_deaths_converge
from due_work_harness.profiles.automatic_recovery import (
    DUE_WORK_PROOFS,
    SELECTION_PROOFS,
    DueWorkSweep,
    assert_published_work_is_recoverable,
    assert_sweep_bindings_are_production_bound,
)
from due_work_harness.profiles.bounded_ownership import (
    FENCED_OWNERSHIP_PROOFS,
    FencedOwnership,
    assert_claim_is_exclusive_across_connections,
    assert_ownership_bindings_are_production_bound,
)
from due_work_harness.profiles.catalog import SAFETY_PROFILES, ConvergenceFamily, Profile
from due_work_harness.profiles.coverage import AssessmentState
from due_work_harness.profiles.crash_ambiguity import (
    AMBIGUITY_PROOFS,
    AmbiguityAware,
    assert_ambiguity_bindings_are_production_bound,
)
from due_work_harness.profiles.durable_retention import (
    RETENTION_PROOFS,
    Retention,
)
from due_work_harness.profiles.eventual_convergence import (
    CONVERGENT_WRITE_PROOFS,
    SNAPSHOT_PROOFS,
    ConvergentWrite,
    SupersededSnapshot,
    assert_convergence_bindings_are_production_bound,
)
from due_work_harness.profiles.fact_derived_obligations import (
    STATE_DERIVED_PROOFS,
    StateDerived,
    assert_derivation_bindings_are_production_bound,
)
from due_work_harness.profiles.gated_execution import (
    ELIGIBILITY_PROOFS,
    ExecutionGateBinding,
    assert_gate_is_recovered_by_the_contract_sweep,
)
from due_work_harness.profiles.harmless_replay import (
    REPLAY_SAFETY_PROOFS,
    ReplaySafeEffect,
)
from due_work_harness.profiles.indivisible_admission import ADMISSION_PROOFS, AdmissionAtomicity
from due_work_harness.profiles.job_retry_limits import (
    BOUNDED_RETRY_PROOFS,
    BoundedRetry,
)


class Adoption(Enum):
    """
    Which gap policy a contract is under. The strict one is the default.

    ``NEW_FEATURE`` forbids gap declarations entirely: a feature shipping with
    its contract must pass every claimed profile. ``LEGACY`` — adopting code
    that predates its contract — permits strict-xfail gap declarations, which
    record findings separately from their fixes.
    """

    NEW_FEATURE = "new feature"
    LEGACY = "legacy adoption"


class DueWorkSource(HarnessModel):
    """
    One production callable whose post-commit work this suite accounts for.

    The callable is load-bearing: it makes the declaration type-checked and
    refactor-aware in the adopter itself, and it gives static tooling something
    to match. A scan that parses production code for after-commit callbacks and
    test code for ``DueWorkSource(...)`` declarations can pair them by exact
    qualified name without importing either, so a publisher nobody accounted
    for fails a check instead of going unnoticed.

    ``sites`` is normally one. It exists for the uncommon case where one
    callable deliberately contains several after-commit callbacks; a scan that
    counts them per callable then notices when another is added, and the
    contract is reviewed again.
    """

    #: Not validated by Pydantic, so a non-callable is refused below as a design error.
    callable: SkipValidation[Callable[..., object]]
    sites: int = 1

    #: Run the publisher with its own dispatch suppressed, and return the
    #: identity of the work it stranded. Required when the contract claims
    #: profile A: covering this callable says its message can be lost and this
    #: contract insures it, and until that is run the claim is bookkeeping — a
    #: contract can name one publisher and recover a different table entirely.
    publish: Callable[[], Any] | None = None

    #: Age the stranded identity past the sweep's recovery delay.
    make_recovery_eligible: Callable[[Any], None] | None = None

    #: Why this covered source's work cannot be run through the sweep — the
    #: publisher belongs to a different lifecycle than the one this profile A
    #: recovers, and something else demonstrably owns it. A declaration, so a
    #: split between publication and recovery is reviewable rather than silent.
    unrecoverable_because: str | None = None

    def __init__(self, function: Callable[..., object] = MISSING, /, **data: Any) -> None:
        super().__init__(**with_positional(data, callable=function))

    def model_post_init(self, _context: Any) -> None:
        if not callable(self.callable):
            raise DueWorkContractDesignError("DueWorkSource.callable must be callable")
        if self.sites < 1:
            raise DueWorkContractDesignError("DueWorkSource.sites must be at least one")
        if self.publish is not None and self.make_recovery_eligible is None:
            raise DueWorkContractDesignError(
                f"{self.qualified_name}: `publish` is declared without `make_recovery_eligible`, "
                f"so the stranded work can never be aged into the recovery window"
            )
        if self.publish is not None and self.unrecoverable_because is not None:
            raise DueWorkContractDesignError(
                f"{self.qualified_name}: `publish` and `unrecoverable_because` contradict each other; keep exactly one"
            )
        if self.unrecoverable_because is not None and not self.unrecoverable_because.strip():
            raise DueWorkContractDesignError(
                f"{self.qualified_name}: `unrecoverable_because` is empty. The reason is the substance"
            )

    @property
    def qualified_name(self) -> str:
        """The import-qualified identity used by diagnostics and introspection."""
        module = getattr(self.callable, "__module__", "")
        qualname = getattr(self.callable, "__qualname__", "")
        if not module or not qualname:
            raise DueWorkContractDesignError(
                "DueWorkSource.callable must expose __module__ and __qualname__ so it can be identified"
            )
        return f"{module}.{qualname}"


class Claim(HarnessModel):
    """The domain has this capability; every proof of the profile runs."""

    #: Per-invariant legacy defects: proof ``__name__`` -> the reason it
    #: currently fails. Each becomes a centrally generated strict xfail, so
    #: whoever fixes the production gap is forced to delete its entry here.
    #: Legacy adoption only — a new feature claiming a profile must pass it.
    gaps: Mapping[str, str] = Field(default_factory=dict)


class Decline(HarnessModel):
    """The capability is deliberately absent, for an architectural reason."""

    because: str

    #: Optional executable evidence that the decline is substantive — usually
    #: an assertion that the profile's discriminating invariant genuinely fails
    #: against this domain (for example
    #: :class:`~due_work_harness.gap_probes.DisprovenCapability` running profile
    #: F's discovery proof against an edge-triggered sender).
    prove: Callable[[], None] | None = None

    def __init__(self, because: str = MISSING, /, **data: Any) -> None:
        super().__init__(**with_positional(data, because=because))


class NotApplicable(HarnessModel):
    """The capability's precondition does not exist in this domain at all."""

    because: str

    def __init__(self, because: str = MISSING, /, **data: Any) -> None:
        super().__init__(**with_positional(data, because=because))


class KnownGap(HarnessModel):
    """
    The domain should have this capability and lacks it entirely.

    A legacy finding awaiting its fix. Generates one strict xfail whose
    reason is ``because``. Prefer supplying ``detect``: a probe that fails
    while the gap exists, so the fix trips the strict xfail and forces this
    declaration to flip to :class:`Claim` in the same change.
    """

    because: str
    detect: Callable[[], None] | None = None

    def __init__(self, because: str = MISSING, /, **data: Any) -> None:
        super().__init__(**with_positional(data, because=because))


class NotAssessed(HarnessModel):
    """Visible assessment debt, emitted as strict XFAIL until a guarantee is assessed."""

    because: str = Field(min_length=1, description="Why this profile or proof family still needs assessment.")


Disposition = Claim | Decline | NotApplicable | KnownGap | NotAssessed

_ADOPTER_EVIDENCE_LABELS = (
    "# ARRANGE",
    "# REAL PRODUCTION",
    "# EXTERNAL SEAM",
    "# OBSERVE",
)


def _adopter_annotation_defect(owner: str, binding: Callable[..., Any]) -> str | None:
    """Require line-local review annotations that expose what is real and fake."""
    code = callable_code(binding)
    if code is None:
        return f"{owner} is not inspectable, so its adopter evidence annotations cannot be verified"
    if is_harness_owned(code):
        return None
    try:
        source = inspect.getsource(code)
    except (OSError, TypeError):
        return f"{owner} source is unavailable, so its adopter evidence annotations cannot be verified"
    missing = [label for label in _ADOPTER_EVIDENCE_LABELS if label not in source]
    if not missing:
        return None
    return (
        f"{owner} is missing adopter evidence annotations {missing}. Every domain binding "
        "must annotate ARRANGE, REAL PRODUCTION, EXTERNAL SEAM, and OBSERVE beside "
        "the actual callbacks so review can see "
        "which code is test setup, which callable is production, what is substituted, "
        "and which externally meaningful result proves the operation ran"
    )


#: One factory per independent effect; named bindings reuse the same profile engine.
type ReplayBinding = Callable[[], ReplaySafeEffect | AbstractContextManager[ReplaySafeEffect]]
type RetryBinding = Callable[[], BoundedRetry | AbstractContextManager[BoundedRetry]]


class SafetyContract(HarnessModel):
    """Complete replay/retry assessment for one production effect."""

    name: str
    profiles: Mapping[Profile, Disposition]
    adoption: Adoption = Adoption.NEW_FEATURE
    replay: ReplayBinding | Mapping[str, ReplayBinding] | None = None
    retry: RetryBinding | Mapping[str, RetryBinding] | None = None
    fixtures: tuple[str, ...] = ()
    transactional: bool = False

    def model_post_init(self, _context: Any) -> None:
        errors = _safety_design_errors(self)
        if errors:
            raise DueWorkContractDesignError(
                f"{self.name}: the safety contract declaration is incomplete or contradictory — "
                "fix the declaration before any behavioral proof can mean anything:\n- " + "\n- ".join(errors)
            )


class ExtraProof(HarnessModel):
    """
    One domain-specific application of a standalone harness proof.

    For the proofs that need adopter-specific injection or configuration —
    :func:`~due_work_harness.profiles.automatic_recovery.assert_one_failing_row_does_not_stall_the_tick`,
    :func:`~due_work_harness.helpers.assert_provider_call_holds_no_transaction`,
    :func:`~due_work_harness.profiles.bounded_ownership.assert_the_lease_outlives_the_work`,
    the cost and waste proofs — wrapped as a zero-arg closure. The closure should *call* a
    harness proof with domain bindings, not restate its assertions.
    """

    name: str
    run: Callable[[], None]

    #: Give the generated test the host's transactional database marks (real
    #: commits, no test-wrapping transaction) — required by proofs that observe
    #: transaction state or race real connections.
    transactional: bool = False

    #: Known legacy defect: the reason this proof currently fails. Becomes a
    #: strict xfail, same discipline as :attr:`Claim.gaps`.
    gap: str | None = None

    #: Pytest fixtures the generated test must request first.
    fixtures: tuple[str, ...] = ()

    #: Why this extra reaches no production callable. Required when ``run``
    #: references none, so the omission is a declaration rather than a hole:
    #: an extra that feeds a shared proof entirely test-authored bindings
    #: generates a green case certifying nothing, and every other binding in
    #: this contract is guarded against exactly that. A genuine policy proof —
    #: a lease interval measured against a task time limit — reaches only
    #: constants and says so here.
    no_production_callable_because: str | None = None


#: One canonical mapping from guarantees to bindings; E has four independently assessed families.
_PROFILE_BINDINGS: dict[Profile, tuple[str, ...]] = {
    Profile.AUTOMATIC_RECOVERY: ("sweep",),
    Profile.BOUNDED_OWNERSHIP: ("ownership",),
    Profile.CRASH_AMBIGUITY: ("ambiguity",),
    Profile.DURABLE_RETENTION: ("retention",),
    Profile.EVENTUAL_CONVERGENCE: ("convergence", "snapshot", "in_flight", "evidence_confluence"),
    Profile.FACT_DERIVED_OBLIGATIONS: ("derivation",),
    Profile.G: ("eligibility",),
    Profile.H: ("replay",),
    Profile.I: ("admission",),
    Profile.J: ("retry",),
}

#: The proofs each binding field is measured by. Each list opens with the
#: field's invariant-0 binding guard, so a counterfeit binding fails before any
#: behavioral proof lends it credibility; the raw ``*_PROOFS`` tuples stay
#: behavioral so the harness's own mutation catalogs measure behavior only.
_BINDING_PROOFS: dict[str, tuple[Callable[[Any], None], ...]] = {
    "sweep": (assert_sweep_bindings_are_production_bound, *DUE_WORK_PROOFS),
    "ownership": (
        assert_ownership_bindings_are_production_bound,
        *FENCED_OWNERSHIP_PROOFS,
        assert_claim_is_exclusive_across_connections,
    ),
    "ambiguity": (assert_ambiguity_bindings_are_production_bound, *AMBIGUITY_PROOFS),
    "retention": RETENTION_PROOFS,
    "convergence": (assert_convergence_bindings_are_production_bound, *CONVERGENT_WRITE_PROOFS),
    "snapshot": SNAPSHOT_PROOFS,
    "in_flight": (),  # Scenario owns generated histories, not a flat assertion tuple.
    "evidence_confluence": (),
    "eligibility": ELIGIBILITY_PROOFS,
    "admission": ADMISSION_PROOFS,
    "replay": REPLAY_SAFETY_PROOFS,
    "retry": BOUNDED_RETRY_PROOFS,
    "derivation": (assert_derivation_bindings_are_production_bound, *STATE_DERIVED_PROOFS),
}


def _binding_proofs(field_name: str) -> tuple[Callable[[Any], None], ...]:
    """
    A binding field's proofs: the core's, plus the host's for a sweep.

    Some profile A proofs need a framework — the lifecycle-state proofs read an
    ORM query's WHERE clause — so the host that has the framework supplies
    them (``Host.sweep_proofs``) and the core never imports it.
    """
    proofs = _BINDING_PROOFS[field_name]
    return (*proofs, *current_host().sweep_proofs) if field_name == "sweep" else proofs


#: Proofs that must run with the host's transactional database marks.
_TRANSACTIONAL_PROOFS = frozenset(
    {
        "assert_claim_is_exclusive_across_connections",
        # A genuine inline worker must run on a second database connection so
        # the test can hold its provider boundary open while the harness runs
        # a competing recovery tick. A test-wrapping transaction leaves the
        # setup uncommitted, and the worker's connection cannot observe it.
        "assert_in_flight_work_is_not_duplicated",
    }
)

#: Proofs a gap declaration may NOT name — the meta-invariants that establish
#: the bindings describe production at all. Every behavioral proof is only as
#: true as these, so xfailing one would strict-xfail the defense itself and
#: turn the rest of the profile into measurements of whatever the adapter
#: says. A domain that cannot production-bind a semantic field does not get to
#: claim the profile with the guard waived; it declines, or records a
#: :class:`KnownGap`, or restructures until the binding exists.
UNWAIVABLE_PROOFS = frozenset(
    {
        # Profile A: authorship (in-tuple) and delegation (layer) guards.
        "assert_the_adapter_does_not_author_the_selection",
        "assert_the_adapter_does_not_author_the_tick",
        "assert_the_tick_selects_what_the_adapter_describes",
        "assert_all_owed_variants_are_recovered",
        "assert_every_lifecycle_state_is_declared",
        "assert_sweep_bindings_are_production_bound",
        # Profile B.
        "assert_ownership_transitions_are_production_bound",
        "assert_ownership_bindings_are_production_bound",
        # Profile C.
        "assert_ambiguity_transitions_are_production_bound",
        "assert_ordinary_due_work_is_production_bound",
        "assert_ambiguity_bindings_are_production_bound",
        # Profile D.
        "assert_retention_transition_is_production_bound",
        # Profile E.
        "assert_evidence_application_is_production_bound",
        "assert_convergence_bindings_are_production_bound",
        "assert_snapshot_transitions_are_production_bound",
        # H/J use the same unwaivable binding-integrity policy as A–G.
        "assert_replay_transition_is_production_bound",
        "assert_retry_selection_is_production_bound",
        "assert_retry_runner_is_production_bound",
        "assert_gate_bindings_are_production_bound",
        "assert_admission_is_production_bound",
        # Profile F.
        "assert_derived_transitions_are_production_bound",
        "assert_settlement_is_production_bound",
        "assert_outstanding_selection_is_production_bound",
        "assert_derivation_bindings_are_production_bound",
    }
)

#: The A↔F coherence proof: generated when a contract claims BOTH profiles,
#: declared as a gap under either one's ``Claim.gaps`` (a genuinely split
#: recovery is a real, reviewable finding — unlike the binding guards above).
_COHERENCE_PROOF_NAME = "assert_automatic_recovery_consumes_derived_obligations"


class DueWorkContract(HarnessModel):
    """
    One domain's durable work, declared completely.

    Bindings are zero-arg factories called fresh per generated test. A factory
    may return the profile's binding model directly, or a context manager
    yielding it — the shape for bindings that need to patch a dispatch path or
    install a recorder for the duration of a proof.
    """

    name: str

    #: A disposition for EVERY :class:`Profile` member. Silence is not valid:
    #: an omitted profile is a design error, because it is exactly the omitted
    #: profile that would have revealed the defect.
    profiles: Mapping[Profile, Disposition]

    #: Competing-event E families; each scenario owns its deterministic histories.
    in_flight: Mapping[str, InstanceOf[InFlightConvergence]] = Field(default_factory=dict)
    evidence_confluence: Mapping[str, InstanceOf[EvidenceConfluence]] = Field(default_factory=dict)
    #: H: replay one logical operation and observe its external result.
    replay: ReplayBinding | Mapping[str, ReplayBinding] | None = None
    #: J: drive transient failures until the production retry budget is exhausted.
    retry: RetryBinding | Mapping[str, RetryBinding] | None = None
    #: I: standalone commands interrupted after partial product and obligation writes.
    admission: Mapping[str, Callable[[], AdmissionAtomicity | AbstractContextManager[AdmissionAtomicity]]] = Field(
        default_factory=dict
    )
    #: Independent E-family assessments. Unbound, undecided families generate strict XFAILs.
    convergence_families: Mapping[ConvergenceFamily, Disposition] = Field(default_factory=dict)

    #: Which gap policy applies. The default, ``NEW_FEATURE``, forbids gap
    #: declarations entirely — a new feature must pass every claimed profile in
    #: the change that introduces it. Declare ``Adoption.LEGACY`` only when binding code that
    #: predates its contract; that declaration is deliberately visible in the
    #: diff, so waiving the policy is a reviewable act rather than a default.
    adoption: Adoption = Adoption.NEW_FEATURE

    #: Profile A: the domain's recovery sweep.
    sweep: Callable[[], DueWorkSweep | AbstractContextManager[DueWorkSweep]] | None = None

    #: Profile B: the domain's ownership mechanics.
    ownership: Callable[[], FencedOwnership | AbstractContextManager[FencedOwnership]] | None = None

    #: Profile C: the domain's ambiguity handling.
    ambiguity: Callable[[], AmbiguityAware | AbstractContextManager[AmbiguityAware]] | None = None

    #: Profile D: the domain's retention pass.
    retention: Callable[[], Retention | AbstractContextManager[Retention]] | None = None

    #: Profile E, state half: the domain's result-application function.
    convergence: Callable[[], ConvergentWrite | AbstractContextManager[ConvergentWrite]] | None = None

    #: Profile E, worker half: the domain's superseded-snapshot guard.
    snapshot: Callable[[], SupersededSnapshot | AbstractContextManager[SupersededSnapshot]] | None = None

    #: Profile F: the domain's derivation of obligations from product state.
    derivation: Callable[[], StateDerived | AbstractContextManager[StateDerived]] | None = None

    #: Optional execution gate: work that is owed but blocked by something the
    #: product decides (see :mod:`due_work_harness.profiles.gated_execution`),
    #: independent of tables and worker framework. One factory, or named
    #: factories for named blockers; each generated proof gets a fresh blocked
    #: example with its readiness notification lost. Native recovery may stand alone;
    #: claiming A also generates the gate/sweep composition proof.
    eligibility: ExecutionGateBinding | Mapping[str, ExecutionGateBinding] | None = None

    #: Domain-specific applications of the standalone proofs.
    extras: tuple[ExtraProof, ...] = ()

    #: Production transitions that commit work and hand work off. Each
    #: generates one transactional case: with every notification lost, and with
    #: the worker dying right after each of the transition's commits, recovery
    #: must reach the outcome of normal operation with notifications delivered
    #: (see :mod:`due_work_harness.crash_histories`). Requires
    #: :attr:`handoff_delivery`.
    #: Bare ``HandoffHistory`` (implicitly ``[Any, Any]``): Pydantic would rebuild an
    #: unparametrized history as a new ``HandoffHistory[Any, Any]`` instance.
    handoffs: tuple[HandoffHistory, ...] = ()

    #: How the handoffs' published work reaches a worker: delivered, lost, and
    #: recovered the way production recovers it (a recovery tick, a job queue's
    #: stalled-job reclaim, a workflow relaunch). Every crash history runs
    #: through it, so declaring ``handoffs`` without it is a design error —
    #: without a recovery path there is nothing to converge with.
    #: A protocol implementation, so Pydantic does not check it.
    handoff_delivery: SkipValidation[Delivery | None] = None

    #: Production transitions run as real processes that really die (see
    #: :mod:`due_work_harness.process_histories`): a worker in a child process, an
    #: application restarted. Each generates one case, beside ``handoffs``, and
    #: recovers through its own ``recover``; no ``handoff_delivery`` is needed.
    #: Bare ``ProcessHistory`` for the same reason as ``handoffs``.
    process_handoffs: tuple[ProcessHistory, ...] = ()

    #: Legacy findings on named handoffs, in-process or process: ``{history name:
    #: reason}``. Each becomes a strict xfail, under the same ``adoption`` policy
    #: as gaps. The xfail accepts only a divergence
    #: (:class:`~due_work_harness.crash_histories.HistoriesDiverged`), and when the
    #: history declares its ``findings`` table, only the divergence that table
    #: pins: a binding that breaks, a positive control that fails, or a finding
    #: that moved, fails the case.
    handoff_gaps: Mapping[str, str] = Field(default_factory=dict)

    #: Pytest fixtures every generated behavioral test must request.
    fixtures: tuple[str, ...] = ()

    #: Executors that require a committed claim cannot run inside a
    #: test-wrapping rollback transaction. Give every generated runtime proof,
    #: including source-loss and composition cases, the host's transactional
    #: database marks.
    transactional: bool = False

    def model_post_init(self, _context: Any) -> None:
        assert not self.in_flight.keys() & self.evidence_confluence.keys(), "duplicate interleaving scenario name"
        for scenarios in (self.in_flight, self.evidence_confluence):
            interleaving_integration.validate(scenarios, legacy=self.adoption == Adoption.LEGACY)
        errors = _design_errors(self)
        if errors:
            raise DueWorkContractDesignError(
                f"{self.name}: the due-work contract declaration is incomplete "
                f"or contradictory — fix the declaration before any behavioral "
                f"proof can mean anything:\n- " + "\n- ".join(errors)
            )


def _bound_fields(contract: DueWorkContract | SafetyContract, profile: Profile) -> list[str]:
    return [name for name in _PROFILE_BINDINGS[profile] if getattr(contract, name, None)]


def _factories(contract: DueWorkContract | SafetyContract, field_name: str) -> Mapping[str, Callable[[], Any]]:
    """Normalize singular/named bindings at the actual reflective declaration boundary."""
    value = getattr(contract, field_name)
    if field_name in ("in_flight", "evidence_confluence"):
        return {name: scenario.bind for name, scenario in value.items()}
    return value if isinstance(value, Mapping) else {"": value}


def convergence_assessments(contract: DueWorkContract) -> Mapping[ConvergenceFamily, Disposition]:
    """Return each E family's actual binding claim or an explicit visible coverage omission."""
    result = {}
    for family in ConvergenceFamily:
        declared = contract.convergence_families.get(family)
        bound = bool(getattr(contract, family.value))
        if declared is not None:
            result[family] = declared
        elif bound:
            result[family] = Claim()
        elif isinstance(contract.profiles.get(Profile.E), Claim):
            result[family] = NotAssessed(
                because="No production binding or applicability decision is declared for this E family."
            )
        else:
            result[family] = contract.profiles[Profile.E]
    return result


def _bespoke_assertion_defect(owner: str, kind: str, binding: Any) -> str | None:
    """
    Why a probe/extra binding re-introduces bespoke invariant logic, or None.

    The declarative layer exists to keep invariant logic out of adopters, so a
    ``detect``, ``prove``, or ``ExtraProof.run`` must either be root-owned (a
    typed probe from :mod:`.gap_probes`, or any harness-defined callable) or
    visibly delegate to a shared ``assert_*`` proof. A heuristic, like
    invariant 0a, and checked the same way — on the binding's code, which is
    what separates "binds production to a shared proof" from "restates the
    assertion locally".

    Two counterfeits the first form of this check accepted are refused now:

    * *Delegation by name.* Matching any referenced name starting with
      ``assert_`` let a test module mint its own ``assert_anything`` and pass.
      The referenced callables are resolved instead, and at least one must be
      a real harness-defined proof.
    * *Local inversion.* A test-authored ``pytest.raises`` (or a swallowed
      ``AssertionError``) around a shared proof turns "the proof fails" into a
      pass — against whatever binding the test constructed, which is how a
      decline gets "proven" against an implementation that never existed.
      Inversion is legitimate and belongs root-side, where
      :class:`~.gap_probes.DisprovenCapability` first forces the bindings
      through the profile's invariant-0 guards.
    """
    code = callable_code(binding)
    if code is None:
        return (
            f"{owner}: {kind} is not a plain Python callable, so whether it "
            f"delegates to a shared proof cannot be checked"
        )
    # Root-owned: defined in the harness package itself (the gap_probes case)
    # or in production code.
    if is_harness_owned(code):
        return None
    if not is_test_code(code):
        return None
    inverted = authored_inversion_names(binding)
    if inverted:
        return (
            f"{owner}: {kind} inverts or swallows an assertion in test code "
            f"(references {sorted(inverted)}). A test-side inversion can "
            f"'prove' any decline by feeding a shared proof a synthetic "
            f"binding and celebrating the failure. Use "
            f"due_work_harness.gap_probes.DisprovenCapability, which owns the "
            f"inversion and first runs the profile's invariant-0 guards on "
            f"the binding it is given"
        )
    if references_harness_assertion(binding):
        return None
    return (
        f"{owner}: {kind} is a hand-rolled check in test code that delegates "
        f"to no harness-defined assert_* proof. The root owns invariant logic "
        f"— use a typed probe from due_work_harness.gap_probes (a recurring gap shape "
        f"belongs there, with one owner and self-tests), or call a shared "
        f"proof with domain bindings. A locally-defined helper merely NAMED "
        f"assert_* does not count, and a domain-specific behavioral test "
        f"belongs in a plain test module, not inside the contract declaration"
    )


def _decline_proof_defect(owner: str, binding: Any) -> str | None:
    """A negative claim must come from production-owned absence semantics."""
    authored = is_test_authored(binding)
    if authored is None:
        return f"{owner}: the Decline negative proof is not an inspectable Python callable"
    if not authored:
        return None
    return (
        f"{owner}: the Decline negative proof is test-authored. A test can make "
        "any capability look absent by substituting a no-op or copied query; "
        "bind a production absence/probe owned by runtime or by the root harness"
    )


def _profile_design_errors(contract: DueWorkContract | SafetyContract, scope: tuple[Profile, ...]) -> list[str]:
    """One claim/decline/gap and binding-integrity policy for every guarantee and contract scope."""
    errors: list[str] = []
    unknown = set(contract.profiles) - set(scope)
    if unknown:
        errors.append(f"profiles outside this contract scope: {unknown}")
    missing = [profile for profile in scope if profile not in contract.profiles]
    if missing:
        errors.append(
            f"no disposition for profile(s) {', '.join(p.name for p in missing)}. "
            f"Silence is not a disposition — claim it, decline it with a reason, "
            f"mark it not applicable, or record it as a known gap"
        )

    for profile in scope:
        disposition = contract.profiles.get(profile)
        if disposition is None:
            continue
        bound = _bound_fields(contract, profile)
        if isinstance(disposition, Claim):
            if not bound:
                fields = " or ".join(_PROFILE_BINDINGS[profile])
                errors.append(
                    f"profile {profile.name} ({profile.value}) is claimed but has no binding — supply `{fields}=`"
                )
                continue
            valid = {proof.__name__ for name in bound for proof in _binding_proofs(name)}
            if profile is Profile.G:
                valid.add("assert_gate_is_recovered_by_the_contract_sweep")
            if (
                isinstance(contract, DueWorkContract)
                and profile in (Profile.A, Profile.F)
                and contract.sweep is not None
                and contract.derivation is not None
            ):
                valid.add(_COHERENCE_PROOF_NAME)
            for gap_name, reason in disposition.gaps.items():
                if gap_name in UNWAIVABLE_PROOFS:
                    errors.append(
                        f"profile {profile.name}: gap {gap_name!r} names a "
                        f"binding-integrity proof, which cannot be waived — an "
                        f"xfail there would strict-xfail the defense itself and "
                        f"leave every behavioral proof measuring whatever the "
                        f"adapter says. Production-bind the field, or stop "
                        f"claiming the profile"
                    )
                elif gap_name not in valid:
                    errors.append(
                        f"profile {profile.name}: gap {gap_name!r} names no proof "
                        f"that will run. If a proof was renamed, rename its gap "
                        f"entry with it — dropping the mark would turn a "
                        f"documented gap into an unexplained failure"
                    )
                if not reason.strip():
                    errors.append(f"profile {profile.name}: gap {gap_name!r} has an empty reason")
            for field_name in bound:
                for scenario_name, binding in _factories(contract, field_name).items():
                    defect = _adopter_annotation_defect(
                        f"profile {profile.name} `{field_name}` {scenario_name} binding", binding
                    )
                    if defect:
                        errors.append(defect)
        else:
            because = disposition.because
            if not because.strip():
                errors.append(
                    f"profile {profile.name} is {type(disposition).__name__} with no "
                    f"reason. The reason is the substance — without it a decline is "
                    f"indistinguishable from nobody having got to it"
                )
            if bound:
                errors.append(
                    f"profile {profile.name} is {type(disposition).__name__} but "
                    f"`{', '.join(bound)}=` is bound. A binding for a profile the "
                    f"contract does not claim is a contradiction — either claim the "
                    f"profile or drop the binding"
                )
            if isinstance(disposition, KnownGap) and disposition.detect is not None:
                defect = _bespoke_assertion_defect(
                    f"profile {profile.name}", "the KnownGap detect probe", disposition.detect
                )
                if defect:
                    errors.append(defect)
            if isinstance(disposition, Decline) and disposition.prove is not None:
                defect = _decline_proof_defect(f"profile {profile.name}", disposition.prove)
                if defect:
                    errors.append(defect)

    return errors


def _safety_design_errors(contract: SafetyContract) -> list[str]:
    errors = _profile_design_errors(contract, SAFETY_PROFILES)
    if not contract.name.strip():
        errors.append("the safety contract has no name")
    errors.extend(_gap_policy_violations(contract))
    return errors


def eligibility_bindings(contract: DueWorkContract) -> Mapping[str, ExecutionGateBinding]:
    """Named product blockers; the empty internal key keeps a single gate's case ids unprefixed."""
    if contract.eligibility is None:
        return {}
    if isinstance(contract.eligibility, Mapping):
        return contract.eligibility
    return {"": contract.eligibility}


def _eligibility_errors(contract: DueWorkContract) -> list[str]:
    """Every way an ``eligibility=`` declaration fails to describe gates that will run."""
    if contract.eligibility is None:
        return []
    errors: list[str] = []
    disposition = contract.profiles.get(Profile.G)
    if (
        isinstance(disposition, Claim)
        and "assert_gate_is_recovered_by_the_contract_sweep" in disposition.gaps
        and not _claims_recovery(contract)
    ):
        errors.append("a gate/sweep composition gap requires claimed automatic recovery with a sweep")
    if isinstance(contract.eligibility, Mapping):
        if not contract.eligibility:
            errors.append("eligibility variants cannot be empty")
        if any(not name.strip() for name in contract.eligibility):
            errors.append("eligibility variants need nonempty names")
    for name, binding in eligibility_bindings(contract).items():
        defect = _adopter_annotation_defect(f"the eligibility {name + ' ' if name else ''}binding", binding)
        if defect:
            errors.append(defect)
    return errors


def _design_errors(contract: DueWorkContract) -> list[str]:
    """Every way the declaration fails to describe a complete contract."""
    errors: list[str] = _eligibility_errors(contract)
    if not contract.name.strip():
        errors.append("the contract has no name")
    if contract.handoffs and contract.handoff_delivery is None:
        errors.append(
            "handoff histories are declared without `handoff_delivery=`. Every crash history loses "
            "or delivers what its transition published and then runs production's recovery through "
            "that Delivery, so without one there is nothing to recover with. Supply the Delivery "
            "that runs this domain's real recovery (CallableDelivery(name, recover=...) covers most)"
        )
    if contract.handoff_delivery is not None and not contract.handoffs:
        errors.append(
            "`handoff_delivery=` is set but no handoff history is declared, so nothing would run "
            "through it. Declare the handoffs it recovers, or drop the delivery"
        )
    histories: list[HandoffHistory[Any, Any] | ProcessHistory[Any, Any]] = [
        *contract.handoffs,
        *contract.process_handoffs,
    ]
    names = [history.name for history in histories]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        errors.append(f"handoff history names must be unique: {duplicated}")
    unknown_gaps = sorted(set(contract.handoff_gaps) - set(names))
    if unknown_gaps:
        errors.append(f"handoff_gaps name no declared handoff history: {unknown_gaps}")
    for history in histories:
        if history.findings is None:
            continue
        gap = history.name in contract.handoff_gaps
        if history.findings.outcomes and not gap:
            errors.append(
                f"handoff {history.name!r} declares findings in which histories diverge, but no handoff_gaps entry: "
                f"a divergence the table pins fails the verdict. Declare the gap with its reason"
            )
        if gap and not history.findings.outcomes:
            errors.append(
                f"handoff {history.name!r} declares a gap, but its findings table has no divergent history, so "
                f"the gap could never be the reason it fails. Pin what diverges, or drop the gap"
            )
    for family, assessment in contract.convergence_families.items():
        if not isinstance(family, ConvergenceFamily):
            errors.append(f"unknown convergence family {family!r}")
            continue
        if isinstance(assessment, Claim) != bool(getattr(contract, family.value)):
            errors.append(f"convergence family {family.name} assessment contradicts its binding")
        if not isinstance(assessment, Claim) and not assessment.because.strip():
            errors.append(f"convergence family {family.name} needs an assessment reason")
        if isinstance(assessment, KnownGap) or (isinstance(assessment, Claim) and assessment.gaps):
            errors.append("E-family gaps belong to existing profile proof IDs or exact scenario history IDs")
    errors.extend(_profile_design_errors(contract, tuple(Profile)))

    seen: set[str] = set()
    for extra in contract.extras:
        if not extra.name.strip():
            errors.append("an extra proof has no name")
        if extra.name in seen:
            errors.append(f"duplicate extra proof name {extra.name!r}")
        seen.add(extra.name)
        if extra.gap is not None and not extra.gap.strip():
            errors.append(f"extra proof {extra.name!r} has an empty gap reason")
        annotation_defect = _adopter_annotation_defect(
            f"extra proof {extra.name!r} `run` binding",
            extra.run,
        )
        if annotation_defect:
            errors.append(annotation_defect)
        defect = _bespoke_assertion_defect(f"extra proof {extra.name!r}", "run", extra.run)
        if defect:
            errors.append(defect)
        errors.extend(_extra_production_defects(extra))

    errors.extend(_gap_policy_violations(contract))
    return errors


def _extra_production_defects(extra: ExtraProof) -> list[str]:
    """
    An extra must reach production, or say why it cannot.

    The standalone proofs — head-of-line blocking, redispatch waste, idle-tick
    cost, provider-transaction holding — take loose callables rather than a
    typed binding model, so none of the invariant-0 guards the profiles get
    ever ran on them. An ``ExtraProof`` whose closure builds its own rows, its
    own tick and its own observation therefore generated a green case
    certifying nothing, while passing every check this layer had: a unique
    name, four comment labels, and one reference to a harness ``assert_*``.

    Weaker than a profile binding on purpose, and the docstring says so: this
    proves the extra *reaches* production, not that the reference is
    load-bearing. The durable fix is typed binding models for the
    standalone proofs, which would put them under the same guards as every
    profile field. Until then this raises the counterfeit's cost from free to
    deliberate, which is the standard every tripwire here is held to.

    Arrangement is deliberately not checked. Extras legitimately create rows
    and inject faults, so the authorship tripwire would reject the real ones.
    """
    reaches_production = True
    try:
        assert_test_binding_delegates_to_production(
            adopter=f"extra proof {extra.name!r}",
            field="run",
            binding=extra.run,
            production_shape="the production callable the shared proof is about",
        )
    except AssertionError:
        reaches_production = False

    if not reaches_production and extra.no_production_callable_because is None:
        return [
            f"extra proof {extra.name!r}: `run` references no production callable, class, "
            f"or module, so the shared proof it calls would measure test-authored "
            f"bindings and stay green forever. Bind the production callable the proof "
            f"is about — or, for a genuine policy proof over constants, say why none "
            f"exists with `no_production_callable_because=`"
        ]
    if reaches_production and extra.no_production_callable_because is not None:
        return [
            f"extra proof {extra.name!r}: `no_production_callable_because` is set but "
            f"`run` does reach production code. They contradict each other; drop the "
            f"declaration"
        ]
    if extra.no_production_callable_because is not None and not extra.no_production_callable_because.strip():
        return [f"extra proof {extra.name!r}: `no_production_callable_because` is empty. The reason is the substance"]
    return []


def _gap_policy_violations(contract: DueWorkContract | SafetyContract) -> list[str]:
    """The encoded new-feature policy: no gap declarations outside legacy adoption."""
    if contract.adoption is Adoption.LEGACY:
        return []
    declared: list[str] = []
    for profile in contract.profiles:
        disposition = contract.profiles.get(profile)
        if isinstance(disposition, Claim) and disposition.gaps:
            declared.append(f"profile {profile.name} claims with {len(disposition.gaps)} gap(s)")
        if isinstance(disposition, KnownGap):
            declared.append(f"profile {profile.name} is a KnownGap")
    if isinstance(contract, DueWorkContract):
        declared.extend(
            f"extra proof {extra.name!r} declares a gap" for extra in contract.extras if extra.gap is not None
        )
        declared.extend(f"handoff {name!r} declares a gap" for name in contract.handoff_gaps)
    if not declared:
        return []
    return [
        f"adoption is NEW_FEATURE (the default), which forbids gap declarations, "
        f"but the contract declares: {'; '.join(declared)}. A new due-work "
        f"feature must pass every claimed profile in its own PR. Pass "
        f"`adoption=Adoption.LEGACY` ONLY when binding code that predates its "
        f"contract — that declaration is deliberately visible in the diff, so "
        f"waiving the policy is a reviewable act rather than a default"
    ]


class ContractCase(HarnessModel):
    """One generated test: an id, a body, and the fixtures it needs."""

    id: str
    run: Callable[[], None]
    fixtures: tuple[str, ...] = ()

    #: Primary guarantee; bespoke extras have no inferred profile ownership.
    profile: Profile | None = None
    #: Binding or history family used to explain the case in coverage reports.
    family: str = ""
    #: Declaration rows stay visible but never count as passing behavioral proofs.
    assessment: Disposition | None = None
    #: Composition contributes this same execution to each related guarantee.
    related_profiles: tuple[Profile, ...] = ()

    @property
    def assessment_only(self) -> bool:
        """Pure declarations are not execution; real detect/prove callbacks carry evidence.

        An executed gap or decline still cannot verify its profile: coverage's
        assessment state must be claimed before any passing cases can certify it.
        """
        if isinstance(self.assessment, KnownGap):
            return self.assessment.detect is None
        if isinstance(self.assessment, Decline):
            return self.assessment.prove is None
        return self.assessment is not None

    def __repr__(self) -> str:
        return self.id


@contextmanager
def _entered(factory: Callable[[], Any]) -> Iterator[Any]:
    """The binding a factory produces, entering it when it is a context manager."""
    built = factory()
    if isinstance(built, AbstractContextManager):
        with proof_context(built) as binding:
            yield binding
    else:
        yield built


def _proof_runner(factory: Callable[[], Any], proof: Callable[[Any], None]) -> Callable[[], None]:
    @wraps(proof)
    def run() -> None:
        with _entered(factory) as binding:
            proof(binding)

    return run


def _gate_sweep_runner(gate_factory: Callable[[], Any], sweep_factory: Callable[[], Any]) -> Callable[[], None]:
    """The gate is entered first: it arranges the blocked example the sweep then selects, or does not."""

    def run() -> None:
        with _entered(gate_factory) as gate, _entered(sweep_factory) as sweep:
            assert_gate_is_recovered_by_the_contract_sweep(gate, sweep)

    return run


def _coherence_runner(
    sweep_factory: Callable[[], Any],
    derivation_factory: Callable[[], Any],
) -> Callable[[], None]:
    def run() -> None:
        with _entered(sweep_factory) as sweep:
            with _entered(derivation_factory) as derived:
                assert_automatic_recovery_consumes_derived_obligations(sweep, derived)

    return run


def _unclaimed_case(
    label: str,
    disposition: Decline | NotApplicable | KnownGap | NotAssessed,
    transactional: bool,
    fixtures: tuple[str, ...],
) -> Any:
    """
    The visible case for a profile the contract does not claim.

    A decline's ``prove`` and a known gap's ``detect`` run against production,
    so they get the contract's database mode like every other runtime proof: a
    probe that needs real commits must not see an uncommitted test transaction,
    where its own arrangement is invisible to the production path it runs and a
    strict xfail would then pass for the wrong reason.
    """
    if isinstance(disposition, NotAssessed):
        reason = (
            f"Not assessed: {disposition.because} Resolve by adopting the profile/family, "
            "demonstrating a KnownGap, or establishing NotApplicable."
        )
        case = ContractCase(id=f"{label}-not_assessed", run=_documenting_failure(reason))
        # Only this deliberate assessment failure is expected. Unrelated errors
        # and accidental passes must still fail the suite.
        return pytest.param(
            case, id=case.id, marks=pytest.mark.xfail(strict=True, reason=reason, raises=pytest.fail.Exception)
        )
    if isinstance(disposition, Decline):
        case = ContractCase(
            id=f"{label}-declined",
            run=disposition.prove or (lambda: None),
            fixtures=fixtures if disposition.prove else (),
        )
        return pytest.param(case, id=case.id, marks=_database_marks(transactional) if disposition.prove else [])
    if isinstance(disposition, NotApplicable):
        case = ContractCase(id=f"{label}-not_applicable", run=lambda: None)
        return pytest.param(case, id=case.id)
    case = ContractCase(
        id=f"{label}-known_gap",
        run=disposition.detect or _documenting_failure(disposition.because),
        fixtures=fixtures if disposition.detect else (),
    )
    marks = _database_marks(transactional) + [pytest.mark.xfail(strict=True, reason=disposition.because)]
    return pytest.param(case, id=case.id, marks=marks)


def _database_marks(transactional: bool) -> list[Any]:
    """
    The marks a generated case needs to use the database, from the host.

    Read when cases are generated rather than at import, so the host a session
    configures (in ``conftest.py`` or the ``due_work_harness_host`` ini option)
    is the one that decides. ``transactional`` asks for real commits with no
    test-wrapping transaction. The default host returns no marks.
    """
    return list(current_host().database_marks(transactional))


def due_work_database(transactional: bool = True) -> Callable[[Any], Any]:
    """
    Give a hand-written test the database marks the host gives generated cases.

    A findings table or an extra proof written by hand needs the same database
    access as the generated suite: with the Django host, real commits and, where
    the host is configured for it, the serialized rollback that restores seeded
    rows. Taking them from the host keeps the two from drifting apart.
    """

    def apply(test: Any) -> Any:
        for mark in _database_marks(transactional):
            test = mark(test)
        return test

    return apply


def _proof_marks(contract_transactional: bool, proof_name: str) -> list[Any]:
    return _database_marks(contract_transactional or proof_name in _TRANSACTIONAL_PROOFS)


def _documenting_failure(reason: str) -> Callable[[], None]:
    def run() -> None:
        pytest.fail(reason)

    return run


def _assessment_case(
    contract: DueWorkContract | SafetyContract,
    profile: Profile,
    disposition: Decline | NotApplicable | KnownGap | NotAssessed,
    *,
    family: str = "",
) -> Any:
    """Generate one canonical assessment row, including independently unassessed E families."""
    label = f"{profile.case_prefix}-{family}" if family else profile.case_prefix
    row = _unclaimed_case(label, disposition, contract.transactional, contract.fixtures)
    case = row.values[0].model_copy(update=dict(profile=profile, family=family, assessment=disposition))
    return pytest.param(case, id=case.id, marks=row.marks)


def _profile_cases(contract: DueWorkContract | SafetyContract, scope: tuple[Profile, ...]) -> list[Any]:
    """Generate each declared flat proof exactly once with common fixtures and gap handling."""
    params: list[Any] = []
    for profile in scope:
        disposition = contract.profiles[profile]
        if not isinstance(disposition, Claim):
            params.append(_assessment_case(contract, profile, disposition))
            continue
        for field_name in _bound_fields(contract, profile):
            for scenario_name, factory in _factories(contract, field_name).items():
                for proof in _binding_proofs(field_name):
                    base = "eligibility" if profile is Profile.G else profile.case_prefix
                    prefix = f"{base}-{scenario_name}" if scenario_name else base
                    case = ContractCase(
                        id=f"{prefix}-{proof.__name__}",
                        run=_proof_runner(factory, proof),
                        profile=profile,
                        family=field_name,
                        fixtures=contract.fixtures,
                    )
                    marks = _database_marks(
                        contract.transactional or profile is Profile.I or proof.__name__ in _TRANSACTIONAL_PROOFS
                    )
                    reason = disposition.gaps.get(proof.__name__)
                    if reason is not None:
                        marks.append(pytest.mark.xfail(strict=True, reason=reason))
                    params.append(pytest.param(case, id=case.id, marks=marks))
    return params


def safety_contract_cases(contract: SafetyContract) -> list[Any]:
    """A scoped H/J view of the common profile case generator."""
    return _profile_cases(contract, SAFETY_PROFILES)


def contract_cases(contract: DueWorkContract) -> list[Any]:
    """All A–J cases and cross-profile compositions, preserving legacy history identities."""
    params = _profile_cases(contract, tuple(Profile))
    if isinstance(contract.profiles[Profile.E], Claim):
        # A passing sibling must not leave an unfinished E-family assessment silent.
        for family, assessment in convergence_assessments(contract).items():
            if isinstance(assessment, NotAssessed):
                params.append(_assessment_case(contract, Profile.E, assessment, family=family.value))
    for name, binding in eligibility_bindings(contract).items():
        # Native workers can own gating without a periodic sweep. When A is
        # claimed, retain the stronger proof that both bindings use that sweep.
        if not _claims_recovery(contract):
            continue
        prefix = f"eligibility-{name}" if name else "eligibility"
        assert contract.sweep is not None, "claimed automatic recovery must supply its sweep"
        case = ContractCase(
            id=f"{prefix}-assert_gate_is_recovered_by_the_contract_sweep",
            profile=Profile.G,
            family="recovery-composition",
            related_profiles=(Profile.A,),
            run=_gate_sweep_runner(binding, contract.sweep),
            fixtures=contract.fixtures,
        )
        marks = _database_marks(contract.transactional)
        disposition = contract.profiles[Profile.G]
        assert isinstance(disposition, Claim)
        if reason := disposition.gaps.get("assert_gate_is_recovered_by_the_contract_sweep"):
            marks.append(pytest.mark.xfail(strict=True, reason=reason))
        params.append(pytest.param(case, id=case.id, marks=marks))
    params.extend(interleaving_integration.cases(contract.in_flight, contract.fixtures))
    params.extend(interleaving_integration.cases(contract.evidence_confluence, contract.fixtures))
    params.extend(_coherence_cases(contract))
    params.extend(_handoff_cases(contract))
    for extra in contract.extras:
        marks = _database_marks(extra.transactional)
        if extra.gap is not None:
            marks.append(pytest.mark.xfail(strict=True, reason=extra.gap))
        case = ContractCase(
            id=f"extra-{extra.name}",
            run=extra.run,
            fixtures=extra.fixtures + contract.fixtures,
        )
        params.append(pytest.param(case, id=case.id, marks=marks))
    return params


def _handoff_runner(delivery: Delivery, history: HandoffHistory[Any, Any]) -> Callable[[], None]:
    def run() -> None:
        assert_crash_at_every_commit_converges(delivery, history)

    return run


def _gap_mark(contract: DueWorkContract, history: HandoffHistory[Any, Any] | ProcessHistory[Any, Any]) -> list[Any]:
    """
    A declared handoff gap's strict xfail, for the divergence alone.

    Findings or not, only :class:`HistoriesDiverged` is the known gap: a broken
    binding, a positive control that fails or a finding that moved fails the case.
    """
    reason = contract.handoff_gaps.get(history.name)
    if reason is None:
        return []
    return [pytest.mark.xfail(strict=True, reason=reason, raises=HistoriesDiverged)]


def _process_runner(history: ProcessHistory[Any, Any]) -> Callable[[], None]:
    def run() -> None:
        assert_process_deaths_converge(history)

    return run


def _handoff_cases(contract: DueWorkContract) -> list[Any]:
    """One crash-history case per declared handoff, in-process (transactional) or as a real process."""
    params: list[Any] = []
    if contract.handoffs:
        delivery = contract.handoff_delivery
        assert delivery is not None, "DueWorkContract validation requires handoff_delivery with handoffs"
        for history in contract.handoffs:
            case = ContractCase(
                profile=Profile.A,
                family="crash-histories",
                id=f"handoff-{history.name}-assert_crash_at_every_commit_converges",
                run=_handoff_runner(delivery, history),
                fixtures=contract.fixtures,
            )
            params.append(pytest.param(case, id=case.id, marks=_database_marks(True) + _gap_mark(contract, history)))
    for history in contract.process_handoffs:
        case = ContractCase(
            profile=Profile.A,
            family="crash-histories",
            id=f"process-{history.name}-assert_process_deaths_converge",
            run=_process_runner(history),
            fixtures=contract.fixtures,
        )
        marks = _database_marks(contract.transactional) + _gap_mark(contract, history)
        params.append(pytest.param(case, id=case.id, marks=marks))
    return params


def _coherence_cases(contract: DueWorkContract) -> list[Any]:
    """
    The cross-profile case a contract earns by claiming A and F together.

    Each profile certifies its own binding; nothing else compares them. An
    early adoption taught why that matters: profile F's ``outstanding`` was one
    predicate and profile A's ``due_work`` a deliberately narrower one, both
    green, with the rows in the difference recoverable by nobody. Claiming both
    profiles therefore generates one more case that runs F's obligations
    through A's real selection and tick (see :mod:`.coherence`). A reviewed,
    deliberate split is declared as a legacy gap on the coherence proof under
    either profile's claim — a strict xfail with the reason attached, never a
    silent property of two predicates.
    """
    if not (isinstance(contract.profiles.get(Profile.A), Claim) and contract.sweep is not None):
        return []
    if not (isinstance(contract.profiles.get(Profile.F), Claim) and contract.derivation is not None):
        return []

    reason: str | None = None
    for profile in (Profile.A, Profile.F):
        disposition = contract.profiles[profile]
        if isinstance(disposition, Claim):
            reason = disposition.gaps.get(_COHERENCE_PROOF_NAME, reason)
    marks = _database_marks(contract.transactional)
    if reason is not None:
        marks.append(pytest.mark.xfail(strict=True, reason=reason))
    case = ContractCase(
        profile=Profile.F,
        family="recovery-composition",
        related_profiles=(Profile.A,),
        id=f"AF-{_COHERENCE_PROOF_NAME}",
        run=_coherence_runner(contract.sweep, contract.derivation),
        fixtures=contract.fixtures,
    )
    return [pytest.param(case, id=case.id, marks=marks)]


def _install_suite(cls: type, params: list[Any], test_name: str, doc: str) -> type:
    assert not hasattr(cls, test_name), f"{cls.__name__} already defines {test_name}, so the suite would shadow it"

    # Every generated case carries the `due_work` mark, so CI can run exactly the harness's suites.
    @pytest.mark.due_work
    @pytest.mark.parametrize("case", params)
    def run_case(self: Any, case: ContractCase, request: pytest.FixtureRequest) -> None:
        for fixture in case.fixtures:
            request.getfixturevalue(fixture)
        case.run()

    run_case.__name__ = test_name
    run_case.__qualname__ = f"{cls.__qualname__}.{test_name}"
    run_case.__doc__ = doc
    setattr(cls, test_name, run_case)
    return cls


def safety_contract_suite(contract: SafetyContract) -> Callable[[type], type]:
    """Generate the complete replay/retry profile suite for one effect."""
    params = safety_contract_cases(contract)
    doc = (
        f"The {contract.name} safety contract, generated from its declaration. "
        "Each case is one replay/retry invariant, decline, or known gap."
    )

    def decorate(cls: type) -> type:
        cls.__safety_contract__ = contract
        return _install_suite(cls, params, "test_safety_contract", doc)

    return decorate


def suite_cases(contract: DueWorkContract, *, covers: tuple[DueWorkSource, ...] = ()) -> list[Any]:
    """
    Every case :func:`due_work_contract_suite` generates for ``contract``, as ``pytest.param`` values.

    The contract's own cases, the covered publishers' recovery cases and the
    safety contract's cases, in the order the suite runs them: the one list to
    count, filter or report from, so no caller rebuilds it and misses a part.
    """
    _validate_covered_sources(contract.name, covers)
    _validate_covered_recovery(contract, covers)
    params = contract_cases(contract)
    params.extend(_covered_recovery_cases(contract, covers))
    return params


def due_work_contract_suite(
    contract: DueWorkContract,
    *,
    covers: tuple[DueWorkSource, ...] = (),
) -> Callable[[type], type]:
    """
    Generate the contract's whole test suite onto a class.

    The one line an adopter writes::

        @due_work_contract_suite(
            MY_CONTRACT,
            covers=(DueWorkSource(MyService.publish_after_commit),),
        )
        class TestMyDomainDueWork:
            pass
    """
    params = suite_cases(contract, covers=covers)
    doc = (
        f"The {contract.name} due-work contract, generated from its declaration. "
        f"Each case is one invariant, decline, or known gap; the id names it."
    )

    def decorate(cls: type) -> type:
        cls.__due_work_contract__ = contract
        cls.__due_work_sources__ = covers
        cls = _install_suite(cls, params, "test_due_work_contract", doc)
        interleaving_integration.install_exploration(
            cls, {**contract.in_flight, **contract.evidence_confluence}, contract.fixtures
        )
        return cls

    return decorate


def _validate_covered_sources(owner: str, covers: tuple[DueWorkSource, ...]) -> None:
    """Reject contradictory source declarations before pytest collection."""
    names = [source.qualified_name for source in covers]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise DueWorkContractDesignError(f"{owner}: duplicate DueWorkSource declarations: {duplicates}")


def _claims_recovery(contract: DueWorkContract) -> bool:
    return isinstance(contract.profiles.get(Profile.A), Claim) and contract.sweep is not None


def _validate_covered_recovery(contract: DueWorkContract, covers: tuple[DueWorkSource, ...]) -> None:
    """
    A profile-A contract must say what its covered publishers leave behind.

    ``covers`` records an association between a publisher and this contract,
    and by itself nothing proves the association means anything: a contract
    could declare it covers one publisher while its sweep recovers a different
    table, and every generated case would still be green.

    Only enforced when profile A is actually claimed with a sweep. A contract
    that declines A has no recovery to check the association against, and a
    known gap is already a red row.
    """
    if not _claims_recovery(contract):
        return
    undeclared_sources = [
        source.qualified_name for source in covers if source.publish is None and source.unrecoverable_because is None
    ]
    if undeclared_sources:
        raise DueWorkContractDesignError(
            f"{contract.name}: profile A is claimed, so every covered source must say whether this "
            f"sweep recovers the work it publishes. Supply `publish=` and `make_recovery_eligible=` "
            f"— or `unrecoverable_because=` when another lifecycle demonstrably owns it — for: "
            f"{undeclared_sources}. Naming a publisher and never running its stranded work through "
            f"the selection is the association without the meaning"
        )


def _covered_recovery_runner(contract: DueWorkContract, source: DueWorkSource) -> Callable[[], None]:
    def run() -> None:
        assert contract.sweep is not None
        assert source.publish is not None
        assert source.make_recovery_eligible is not None
        with _entered(contract.sweep) as sweep:
            assert_published_work_is_recoverable(
                sweep,
                source_name=source.qualified_name,
                publish=source.publish,
                make_recovery_eligible=source.make_recovery_eligible,
            )

    return run


def _covered_recovery_cases(contract: DueWorkContract, covers: tuple[DueWorkSource, ...]) -> list[Any]:
    """One case per covered publisher whose stranded work the sweep must recover."""
    if not _claims_recovery(contract):
        return []
    params: list[Any] = []
    for source in covers:
        short = source.qualified_name.rsplit(".", 1)[-1]
        if source.publish is None:
            # The declared split collects as a visible row, the way a declined
            # profile does — reviewable, never silently skipped.
            case_id = f"covers-{short}-recovery_declined"
            params.append(pytest.param(ContractCase(id=case_id, run=lambda: None), id=case_id))
            continue
        case = ContractCase(
            profile=Profile.A,
            family="publication",
            id=f"covers-{short}-assert_published_work_is_recoverable",
            run=_covered_recovery_runner(contract, source),
            fixtures=contract.fixtures,
        )
        params.append(pytest.param(case, id=case.id, marks=_database_marks(contract.transactional)))
    return params


def disposition_label(disposition: Disposition) -> AssessmentState:
    """Canonical report labels; no test outcome is inferred from an assessment decision."""
    match disposition:
        case Claim():
            return "claimed"
        case Decline():
            return "declined"
        case NotApplicable():
            return "not applicable"
        case KnownGap():
            return "known gap"
        case NotAssessed():
            return "not assessed"


def contract_report(contract: DueWorkContract) -> str:
    """
    The contract's dispositions as a human-readable block.

    For PR bodies and docs; the generated test ids carry the same information
    in CI output.
    """
    lines = [f"{contract.name} ({contract.adoption.value}):"]
    plan = [parameter.values[0] for parameter in contract_cases(contract)]
    for profile in Profile:
        disposition = contract.profiles[profile]
        label = f"Profile {profile.name} — {profile.title}"
        if isinstance(disposition, Claim):
            count = sum(
                not case.assessment_only and (case.profile is profile or profile in case.related_profiles)
                for case in plan
            )
            lines.append(
                f"  {label}: claimed — {count} generated cases"
                + (
                    f", {len(disposition.gaps)} known gap(s): {', '.join(sorted(disposition.gaps))}"
                    if disposition.gaps
                    else ""
                )
            )
        else:
            lines.append(f"  {label}: {disposition_label(disposition)} — {disposition.because}")
        if profile is Profile.G:
            lines.extend(f"    Eligibility [{name or 'default'}]: claimed" for name in eligibility_bindings(contract))
        if profile is Profile.E:
            for family, assessment in convergence_assessments(contract).items():
                lines.append(
                    f"    {family.name}: {type(assessment).__name__}"
                    + (f" — {assessment.because}" if not isinstance(assessment, Claim) else "")
                )
            lines.extend(interleaving_integration.report(contract.in_flight))
            lines.extend(interleaving_integration.report(contract.evidence_confluence))
        if profile is Profile.A:
            lines.extend(
                f"    Handoff [{history.name}]: declared crash/recovery histories" for history in contract.handoffs
            )
    for extra in contract.extras:
        note = f"known gap — {extra.gap}" if extra.gap else "applied"
        lines.append(f"  Extra {extra.name}: {note}")
    return "\n".join(lines)


class ScheduledSelection(HarnessModel):
    """
    A scheduled selection that is not a full due-work sweep.

    The honest shape for the narrow case: a scheduled query whose behavioral
    lifecycle is owned elsewhere (a service that claims and runs work inline, a
    reconciler with its own deep suite) but whose *selection* still runs on a
    schedule forever and must therefore be production-bound, index-served, and
    primary-routed. Registering one of these is an index audit, not a due-work
    adoption — a domain that owes the full contract declares a
    :class:`DueWorkContract` instead.
    """

    name: str

    #: The unbounded production selection — same contract as
    #: :attr:`~due_work_harness.profiles.automatic_recovery.DueWorkSweep.due_work`.
    #: The plan proofs ask the host for a
    #: :class:`~due_work_harness.host.SelectionInspector` that understands
    #: what it returns.
    due_work: Callable[[], Iterable[Any]]

    #: Executable evidence that something runs this selection recurringly —
    #: a schedule-evidence callable from an integration (for example
    #: ``due_work_harness.integrations.celery.celery_beat_evidence("pkg.task")``)
    #: or the adopter's own read of its scheduler. The core never reads a
    #: framework's settings. Required unless :attr:`unscheduled_because` says why none exists — this
    #: is a contract about a SCHEDULED selection, so silence about the schedule
    #: would exempt the light contract from the completeness rule the full one
    #: enforces.
    assert_scheduled: Callable[[], None] | None = None

    #: Why no schedule evidence is supplied — the selection is driven by
    #: something the harness cannot inspect (an external scheduler, a manual
    #: runbook), stated so the omission is a reviewed declaration rather than
    #: a silently skipped check. Exactly one of this and
    #: :attr:`assert_scheduled` must be set.
    unscheduled_because: str | None = None

    #: Which gap policy applies — same rule as :attr:`DueWorkContract.adoption`:
    #: the NEW_FEATURE default forbids ``gaps``.
    adoption: Adoption = Adoption.NEW_FEATURE

    #: Optional: create a representative dataset (and refresh the planner's
    #: statistics) before the plan-shape proof runs. On an empty table every
    #: plan costs roughly nothing, so the planner's choice is a coin flip — a
    #: primary-key walk can "win" merely because it satisfies an ordering
    #: without a sort, which makes the verdict order-dependent. Seeding the
    #: production shape (most rows match nothing, slivers match each branch)
    #: makes the plan verdict a statement about the real trade-off. Run only
    #: before
    #: :func:`~due_work_harness.profiles.automatic_recovery.assert_selection_is_index_served`.
    populate: Callable[[], None] | None = None

    #: Known legacy defects, proof ``__name__`` -> reason, strict-xfailed.
    gaps: Mapping[str, str] = Field(default_factory=dict)

    #: Pytest fixtures every generated case must request first — for example
    #: one a host needs to give a replica-routing proof a distinct replica.
    fixtures: tuple[str, ...] = ()

    def model_post_init(self, _context: Any) -> None:
        errors: list[str] = []
        annotation_defect = _adopter_annotation_defect(
            "scheduled selection `due_work` binding",
            self.due_work,
        )
        if annotation_defect:
            errors.append(annotation_defect)
        valid = {proof.__name__ for proof in SELECTION_PROOFS}
        unwaivable = sorted(set(self.gaps) & UNWAIVABLE_PROOFS)
        if unwaivable:
            errors.append(
                f"gaps name binding-integrity proofs, which cannot be waived: "
                f"{unwaivable}. An xfail there would strict-xfail the defense "
                f"itself — every other proof of this selection is only as true "
                f"as the authorship check. Production-bind the selection instead"
            )
        unknown = sorted(set(self.gaps) - valid)
        if unknown:
            errors.append(
                f"gaps name proofs that are not in the scheduled-selection contract: {unknown}. Valid names: {sorted(valid)}"
            )
        empty = sorted(name for name, reason in self.gaps.items() if not reason.strip())
        if empty:
            errors.append(f"gaps with empty reasons: {empty}")
        if self.assert_scheduled is None and self.unscheduled_because is None:
            errors.append(
                "no schedule evidence and no unscheduled_because. This is a "
                "contract about a SCHEDULED selection — supply `assert_scheduled=` "
                "(an integration's schedule evidence, such as celery_beat_evidence("
                "<task path>), or your own read of the scheduler) or say why none "
                "exists, so the omission is a declaration rather than a silently "
                "skipped check"
            )
        if self.assert_scheduled is not None and self.unscheduled_because is not None:
            errors.append(
                "both schedule evidence and unscheduled_because are set — they contradict each other; keep exactly one"
            )
        if self.unscheduled_because is not None and not self.unscheduled_because.strip():
            errors.append("unscheduled_because is empty. The reason is the substance of the declaration")
        if self.adoption is not Adoption.LEGACY and self.gaps:
            errors.append(
                f"adoption is NEW_FEATURE (the default), which forbids gap "
                f"declarations, but {len(self.gaps)} gap(s) are declared. Pass "
                f"`adoption=Adoption.LEGACY` ONLY when auditing a selection that "
                f"predates its contract"
            )
        if errors:
            raise DueWorkContractDesignError(
                f"{self.name}: the scheduled-selection declaration is incomplete "
                f"or contradictory:\n- " + "\n- ".join(errors)
            )


def _populated_proof_runner(selection: ScheduledSelection, proof: Callable[[Any], None]) -> Callable[[], None]:
    def run() -> None:
        if selection.populate is not None and proof.__name__ == "assert_selection_is_index_served":
            selection.populate()
        proof(selection)

    return run


def scheduled_selection_cases(selection: ScheduledSelection) -> list[Any]:
    """The selection-only proofs, plus schedule evidence when declared."""
    params: list[Any] = []
    for proof in SELECTION_PROOFS:
        marks = _database_marks(False)
        reason = selection.gaps.get(proof.__name__)
        if reason is not None:
            marks.append(pytest.mark.xfail(strict=True, reason=reason))
        case = ContractCase(
            id=proof.__name__,
            run=_populated_proof_runner(selection, proof),
            fixtures=selection.fixtures,
        )
        params.append(pytest.param(case, id=case.id, marks=marks))
    evidence = selection.assert_scheduled
    if evidence is not None:
        case = ContractCase(id="assert_the_selection_is_actually_scheduled", run=evidence)
        params.append(pytest.param(case, id=case.id))
    else:
        # The declared absence collects as a visible row, the same way a
        # declined profile does: reviewable, never silently skipped.
        case = ContractCase(id="schedule_evidence-declined", run=lambda: None)
        params.append(pytest.param(case, id=case.id))
    return params


def scheduled_selection_suite(
    selection: ScheduledSelection,
    *,
    covers: tuple[DueWorkSource, ...] = (),
) -> Callable[[type], type]:
    """
    Generate the scheduled-selection suite onto a class.

    ::

        @scheduled_selection_suite(
            MY_SELECTION,
            covers=(DueWorkSource(MyService.publish_after_commit),),
        )
        class TestMySelectionStaysIndexServed:
            pass
    """
    _validate_covered_sources(selection.name, covers)
    params = scheduled_selection_cases(selection)
    doc = f"The {selection.name} scheduled-selection contract, generated from its declaration."

    def decorate(cls: type) -> type:
        cls.__due_work_contract__ = selection
        cls.__due_work_sources__ = covers
        return _install_suite(cls, params, "test_scheduled_selection_contract", doc)

    return decorate
