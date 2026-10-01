"""
Profile F of the due-work harness: fact-derived obligations (obligations derived from state).

Profile A asks whether *recorded* work survives a lost message. This profile
asks the question underneath it: **can an obligation exist that nothing ever
recorded?**

The distinction is edge- versus level-triggering, and it is easy to lose by
accident. A sweep that re-derives which rows owe an effect from product state on
every pass reconciles work created by a writer that never heard of the effect at
all. Replacing that derivation with a work row written at each known transition
is an upgrade in everything profile A measures — the row is durable, selectable,
ownable, countable — and a *silent downgrade* here, because the obligation now
exists only if some code path remembered to create it.

That failure shape is the dangerous one, because every other profile stays green
through it. Discovery proofs pass: there is nothing to discover. Ownership and
ambiguity proofs pass: they never get a row. **A green board and a stranded
obligation are perfectly compatible**, which is precisely the silence this
package exists to remove.

Stating derivation as a *profile* rather than as an architectural alternative is
the other half of the point. "Derive work from product state" is often argued
as an architectural alternative to a work-row table; as a
capability an implementation either claims or declines, both can be true at once
— and an implementation that correctly declines it, because "already done" is
not visible in the state that asked for the work, is making a real statement
rather than failing a test.

Two adapter invariants and four behavioral invariants:

0a. **Settlement is production-bound** — the adapter invokes the real
    completion transition instead of writing the terminal fields itself.
0b. **Outstanding work is production-bound** — the adapter invokes the real
    derivation/selection instead of reconstructing its predicate.

1. **An unrecorded obligation is discovered** — product state that implies an
   effect is found even though no work record was ever written for it.
2. **Derivation is idempotent** — a second pass over unchanged state changes
   nothing. Without this, a per-minute sweep churns revisions and resets backoff
   on work that is merely waiting, which presents as activity rather than as a
   fault.
3. **A moved desired state is superseded, not gap-filled** — when product state
   changes so that a *different* effect is now wanted, derivation re-opens the
   obligation carrying the new desired state. An implementation that only fills
   gaps leaves the settled-but-stale case permanently wrong.
4. **Deliberately stopped work is not revived** — cancelled, soft-deleted or
   abandoned product state does not produce an obligation.

Invariants 1 and 4 are a pair, in the same way profile C's ambiguity invariants
are. Derive unconditionally and you satisfy 1 while resurrecting cancelled work
forever; derive nothing and you satisfy 4 while being useless. Only together do
they force a real predicate over product state.

One honest note about invariant 2. An implementation whose *selection is* the
derivation — a queryset over product state, materialising nothing — satisfies it
trivially, and that is correct rather than vacuous: invariant 1 is what forces
the derivation to do real work, and 2 only has teeth for implementations that
write work rows. Read a green 2 as "this adopter has nothing to churn", not as
evidence that churn was tested.

The edge-versus-level framing and the four behavioral invariants came from
adopting a shared work-table library, where replacing derivation with recorded
rows is exactly the tempting upgrade described above. The two adapter
invariants apply the shared semantic-binding rule to this profile. It ships
beside the other profiles rather than beside any one implementation.
"""

from collections.abc import Callable, Collection
from typing import Any

from pytest_obligation.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    SELECTION_AUTHORING_OPERATIONS,
    TRANSITION_AUTHORING_OPERATIONS,
    assert_test_binding_delegates_to_production,
    assert_test_binding_forwards,
)
from pytest_obligation.models import HarnessModel


def _same_identity(identity: Any) -> Any:
    return identity


class StateDerived(HarnessModel):
    """
    One domain's derivation of obligations from product state.

    Deliberately expressed over opaque *identities* rather than rows, because
    the two implementation shapes this has to describe disagree about whether a
    row exists at all. For a sweep whose selection is the derivation, ``derive``
    is a no-op and ``outstanding`` is the selection; for one that materialises
    work rows, ``derive`` is the reconciler and ``outstanding`` reads what it
    wrote. Both are legitimate, and the invariants are the same either way.
    """

    name: str

    #: Create product state that implies an effect is owed, WITHOUT recording
    #: the obligation — no work row, no queue message, no dispatch. Returns the
    #: obligation's identity. This is the whole profile in one member: an
    #: adopter that cannot express "implied but unrecorded" is edge-triggered
    #: and should decline profile F rather than approximate it.
    make_implied_obligation: Callable[[], Any]

    #: Run one derivation pass. May materialise work rows, or do nothing at all
    #: for an implementation whose selection is itself the derivation.
    derive: Callable[[], Any]

    #: The identities currently outstanding. A ``Collection`` rather than a
    #: ``Container`` because invariant 2 iterates it as well as testing
    #: membership, and it must be deterministic: that invariant compares two
    #: evaluations, so an iteration order that can change between calls
    #: weakens the comparison to nothing.
    outstanding: Callable[[], Collection[Any]]

    #: A comparable snapshot of everything a derivation pass could churn for one
    #: identity — revision, attempt count, next-attempt time, ordering key.
    #: Invariant 2 asserts a second pass leaves it equal, so a snapshot that
    #: omits the churnable fields weakens that proof to nothing.
    record_of: Callable[[Any], Any]

    #: Apply the effect for the desired state the obligation currently carries,
    #: so that it is no longer outstanding.
    settle: Callable[[Any], None]

    #: Change the product state so a *different* effect is now desired. Returns
    #: the new desired state, for comparison against :attr:`desired_of`.
    move_desired_state: Callable[[Any], Any]

    #: The desired state the obligation currently carries. Invariant 3 uses this
    #: to tell superseding from gap-filling: both re-open the obligation, only
    #: one re-opens it against the state that is actually wanted.
    desired_of: Callable[[Any], Any]

    #: Create product state that someone deliberately stopped — cancelled,
    #: soft-deleted, abandoned. Returns its identity.
    make_stopped: Callable[[], Any]

    #: True when ``derive`` writes a work record, so there is a meaningful
    #: "before derivation" moment. False when the selection *is* the derivation:
    #: an implied obligation is then outstanding as soon as the product state
    #: exists, and invariant 1's pre-derivation guard cannot apply because there
    #: is nothing to run before. Declared rather than inferred, so an adopter
    #: that materialises records still has to face the guard.
    materialises_records: bool = True

    #: Resolve the obligation identity after ``move_desired_state``. Mutable-row
    #: implementations keep the same identity; immutable-row lifecycles may
    #: replace it with a new row while preserving the logical obligation.
    identity_after_move: Callable[[Any], Any] = _same_identity

    #: Arrange an outstanding identity past Profile A's recovery grace. This is
    #: fault/setup injection, used only by the A/F composition proof.
    make_recovery_eligible: Callable[[Any], None] | None = None


def assert_derived_transitions_are_production_bound(derived: StateDerived) -> None:
    """INVARIANT 0a: derivation and desired-state moves invoke production."""
    for field, binding, shape in (
        ("derive", derived.derive, "the production derivation pass"),
        ("move_desired_state", derived.move_desired_state, "the production desired-state transition"),
    ):
        assert_test_binding_forwards(
            adopter=derived.name,
            field=field,
            binding=binding,
            forbidden=INVOCATION_AUTHORING_OPERATIONS,
            production_shape=shape,
        )


def assert_settlement_is_production_bound(derived: StateDerived) -> None:
    """INVARIANT 0: settling invokes runtime behavior, not a test-side update."""
    assert_test_binding_forwards(
        adopter=derived.name,
        field="settle",
        binding=derived.settle,
        forbidden=TRANSITION_AUTHORING_OPERATIONS,
        production_shape="the production settlement transition",
    )


def assert_outstanding_selection_is_production_bound(derived: StateDerived) -> None:
    """INVARIANT 0b: outstanding work is selected by runtime code."""
    assert_test_binding_forwards(
        adopter=derived.name,
        field="outstanding",
        binding=derived.outstanding,
        forbidden=SELECTION_AUTHORING_OPERATIONS,
        production_shape="the production outstanding-work selection",
    )


def assert_derivation_bindings_are_production_bound(derived: StateDerived) -> None:
    """
    INVARIANT 0c, delegation half: profile F's semantic fields REACH production.

    The authorship half lives in the tuple-resident guards
    (:func:`assert_derived_transitions_are_production_bound`,
    :func:`assert_settlement_is_production_bound`,
    :func:`assert_outstanding_selection_is_production_bound`), which reject ORM
    re-implementations. What they cannot reject is the in-memory counterfeit —
    a plain-Python deriver that authors no ORM at all and passes every
    behavioral proof while production does none of it. This delegation check
    runs at the contract layer and in the composite, where the raw tuple's
    mutation catalogs are not in play. ``derive`` is checked only for the
    materialising shape; the selection-is-derivation shape declares
    ``derive=lambda: None`` legitimately.
    """
    for field, binding in (
        ("move_desired_state", derived.move_desired_state),
        ("settle", derived.settle),
        ("outstanding", derived.outstanding),
    ):
        assert_test_binding_delegates_to_production(
            adopter=derived.name,
            field=field,
            binding=binding,
            production_shape=f"the production {field.replace('_', ' ')} seam",
        )
    if derived.materialises_records:
        assert_test_binding_delegates_to_production(
            adopter=derived.name,
            field="derive",
            binding=derived.derive,
            production_shape="the production derivation/reconciliation pass",
        )


def assert_unrecorded_obligation_is_discovered(derived: StateDerived) -> None:
    """
    INVARIANT 1: an obligation nothing recorded is still found.

    The proof of level-triggering. It fails by construction for any
    edge-triggered implementation, which is the intended discrimination rather
    than a defect in the proof: such an implementation should decline this
    profile, and declining is a statement about where its recovery boundary is.

    What this cannot check is that ``make_implied_obligation`` really went
    through a product-state writer that knows nothing about the effect — an
    adapter that creates the work record itself would pass vacuously. That is
    the same un-enforceable adapter responsibility profile A has with
    ``due_work``, and it is stated here for the same reason: a limit named is a
    limit a reviewer can look for.
    """
    identity = derived.make_implied_obligation()
    if derived.materialises_records:
        assert identity not in derived.outstanding(), (
            f"{derived.name}: make_implied_obligation already produced an "
            f"outstanding obligation before any derivation ran, so this proof "
            f"cannot tell derivation from recording. It must create product "
            f"state only"
        )

    derived.derive()

    assert identity in derived.outstanding(), (
        f"{derived.name}: product state implying an effect was not discovered. "
        f"The obligation therefore exists only when some writer remembers to "
        f"record it, and a writer that does not know about this effect strands "
        f"it silently — every other profile stays green while it does"
    )


def assert_derivation_is_idempotent(derived: StateDerived) -> None:
    """
    INVARIANT 2: a second pass over unchanged state changes nothing.

    Derivation runs on a schedule, so it re-visits the same unchanged state
    forever. If each pass rewrites the obligation, a row that is merely waiting
    has its backoff reset and its revision bumped every interval — which
    presents as activity rather than as a fault, and is invisible until the
    retry budget is gone.
    """
    identity = derived.make_implied_obligation()
    derived.derive()
    assert identity in derived.outstanding(), (
        f"{derived.name}: invariant 2 needs a discovered obligation to observe; invariant 1 is the one to read first"
    )

    before = derived.record_of(identity)
    outstanding_before = list(derived.outstanding())

    derived.derive()

    after = derived.record_of(identity)
    assert after == before, (
        f"{derived.name}: a second derivation pass over unchanged state rewrote "
        f"the obligation ({before!r} -> {after!r}). On a schedule that resets "
        f"backoff and churns revisions for work that is only waiting"
    )
    assert list(derived.outstanding()) == outstanding_before, (
        f"{derived.name}: a second derivation pass changed the outstanding set "
        f"without the product state changing, so derivation duplicates the "
        f"obligations it already created"
    )


def assert_moved_desired_state_is_superseded(derived: StateDerived) -> None:
    """
    INVARIANT 3: derivation re-opens against what is wanted *now*.

    The case an implementation that only fills gaps gets wrong. Once the
    obligation has been settled a gap-filler sees no gap and stops looking, so
    product state that moved afterwards stays permanently unreconciled — and if
    it does re-open the obligation, it can still re-open it carrying the old
    desired state, which is the same bug wearing a green test.
    """
    identity = derived.make_implied_obligation()
    derived.derive()
    derived.settle(identity)
    assert identity not in derived.outstanding(), (
        f"{derived.name}: settle() left the obligation outstanding, so this "
        f"proof cannot distinguish superseding from never having settled"
    )

    wanted = derived.move_desired_state(identity)
    moved_identity = derived.identity_after_move(identity)
    derived.derive()

    assert moved_identity in derived.outstanding(), (
        f"{derived.name}: product state moved after the obligation was settled "
        f"and derivation did not re-open it. A settled obligation against a "
        f"superseded desired state is the one case gap-filling cannot see"
    )
    carried = derived.desired_of(moved_identity)
    assert carried == wanted, (
        f"{derived.name}: the re-opened obligation wants {carried!r} while "
        f"product state wants {wanted!r}. It was gap-filled from the stale "
        f"record rather than derived from current state, so the effect it "
        f"produces is already wrong"
    )


def assert_stopped_work_is_not_revived(derived: StateDerived) -> None:
    """
    INVARIANT 4: cancellation survives the next derivation pass.

    The complement of invariant 1, and the reason that one is not satisfiable by
    deriving unconditionally. A derivation with no stop predicate re-creates
    every cancelled, deleted or abandoned obligation on the next tick, forever —
    the sweep and whoever cancelled the work then overwrite each other on a
    schedule.
    """
    # POSITIVE CONTROL: an ordinary obligation in the same fixture and the same
    # pass. Without it, a derivation that produced nothing at all — inert
    # bindings, a fixture in a state nothing derives from — satisfies the
    # assertion below, and so does a `make_stopped` that built something the
    # derivation was never going to consider.
    control = derived.make_implied_obligation()
    stopped = derived.make_stopped()
    derived.derive()
    assert control in derived.outstanding(), (
        f"{derived.name}: this pass derived no ordinary obligation at all, so "
        f"the stopped one being absent is not evidence of a stop predicate. "
        f"Invariant 1 is the one to read first"
    )
    assert stopped not in derived.outstanding(), (
        f"{derived.name}: derivation produced an obligation for product state "
        f"that was deliberately stopped. Whoever cancels the work and the sweep "
        f"that re-derives it now overwrite each other every interval"
    )


#: Every proof, in the order :func:`assert_state_derived_contract` runs them.
STATE_DERIVED_PROOFS: tuple[Callable[[StateDerived], None], ...] = (
    assert_derived_transitions_are_production_bound,
    assert_settlement_is_production_bound,
    assert_outstanding_selection_is_production_bound,
    assert_unrecorded_obligation_is_discovered,
    assert_derivation_is_idempotent,
    assert_moved_desired_state_is_superseded,
    assert_stopped_work_is_not_revived,
)


def assert_state_derived_contract(derived: StateDerived) -> None:
    """
    Run the binding guard and every invariant against one adopter.

    Prefer the individual proofs when an adopter has a known, documented gap:
    xfail that one invariant with a reason rather than skipping the contract, so
    the remaining guarantees stay enforced.
    """
    assert_derivation_bindings_are_production_bound(derived)
    for proof in STATE_DERIVED_PROOFS:
        proof(derived)
