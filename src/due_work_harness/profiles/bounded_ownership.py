"""
Profile B of the due-work harness: bounded (fenced, leased) ownership.

Discovery (:mod:`.automatic_recovery`) answers *what work is outstanding*. This
profile answers *who owns it right now, and what happens when that owner dies*.

It is the half that hand-rolled implementations get wrong, because every failure
mode needs two processes to observe: a claim that is not exclusive looks fine
under a single-threaded test, and a resurrected worker writing under a stale
token looks fine until the day it happens.

Eight invariants, in four pairs — each refusal beside the acceptance that makes
it mean something:

1. **Exclusive claim** — two claims cannot both take the same row. Proven
   twice: sequentially, and (via
   :func:`assert_claim_is_exclusive_across_connections`) with two real
   connections racing on a barrier.
2. **Fence rotation** — reclaiming a dead owner's row issues a *new* token,
   proven through the production recovery path rather than through a release
   that not every design has. Without rotation, a resurrected owner's token is
   still valid and its write lands.
3a. **Current-token write is accepted** — the fence lets the rightful owner
   through.
3. **Stale-token rejection** — a write carrying a superseded token is refused.
   This is what makes a slow, resurrected worker harmless instead of corrupting.
6a. **Current-token renewal is accepted** — the heartbeat works for the owner
   holding the lease.
4. **Live lease protects the owner** — the reaper cannot take a row whose lease
   has not expired, or two workers run the effect concurrently.
5. **Expired lease permits reclaim** — otherwise a dead owner strands the row
   forever, which is the failure discovery was supposed to fix.
6. **Lease renewal is fenced** — a heartbeat carrying a stale token is refused,
   so a dead owner cannot keep a lease alive from beyond the grave.

Invariants 2, 3 and 6 are what a plain "claimed_at timestamp" ownership scheme
cannot express, which is why a token exists at all.

3a and 6a exist because 3 and 6 are one-sided. Both assert only that a
*superseded* token is refused, which an implementation satisfies by refusing
everything — or by doing nothing, since a Python function with no explicit
``return`` yields ``None``. Measured: an in-memory owner whose ``fenced_write``
and ``renew_lease`` were ``lambda row, token: None`` passed every invariant this
profile had. Profile C states the principle ("either alone is satisfiable by a
degenerate implementation that always picks one branch") and profile E applies
it with ``assert_unsettled_state_converges``; this profile had stated it nowhere
and applied it nowhere. ``None`` is no longer read as a refusal either — see
:func:`_is_refused`.

Optionally stronger
-------------------

Supplying ``observe`` — one lambda returning a comparable summary of the
owner-visible row — upgrades all four token proofs from statements about return
values to statements about effects:

* an accepted write or renewal must **change** the observed state, so an
  implementation that reports success and writes nothing is caught;
* a refused one must leave it **identical**, so a guard that reports failure
  after the update has already landed is caught.

Measured: an owner that refuses stale tokens correctly but does nothing on
success passes all eight invariants without ``observe`` and fails two with it.

It is optional because the observation is per-domain and not every adopter has a
cheap way to summarise its row. A shared work-table library owns the row shape
by construction, so this is close to free for one — which is the point.
"""

import threading
from collections.abc import Callable
from datetime import timedelta  # noqa: F401 - referenced in an annotation
from typing import Any
from uuid import UUID

from due_work_harness.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    assert_test_binding_delegates_to_production,
    assert_test_binding_forwards,
)
from due_work_harness.host import current_host
from due_work_harness.models import HarnessModel


class FencedOwnership(HarnessModel):
    """
    One domain's ownership mechanics, described so the proofs can drive them.

    Every callable takes and returns the domain's own identifiers; the contract
    never assumes a shared model, only that a claim yields an opaque token and
    that mutations can be gated on it.
    """

    name: str

    #: Create exactly one row that is available to be claimed.
    make_claimable: Callable[[], Any]

    #: Claim the next available row. Returns ``(row_id, token)``, or ``None``
    #: when nothing is claimable.
    claim: Callable[[], tuple[Any, UUID] | None]

    #: Any owner-gated mutation. Must return False (or raise) on a stale token.
    #: Deliberately distinct from ``renew_lease`` so invariants 3 and 6 exercise
    #: different code paths.
    fenced_write: Callable[[Any, UUID], bool]

    #: Extend the lease under a token. False (or raise) on a stale token.
    renew_lease: Callable[[Any, UUID], bool]

    #: Force the row's lease into the past, simulating an owner that died.
    expire_lease: Callable[[Any], None]

    #: The reaper's per-row disposition for a row that is not eligible for
    #: reclaim (for example because its lease is still live).
    reclaim_stalled: Callable[[Any], Any | None]

    #: What ``reclaim_stalled`` returns to mean "did nothing". Defaults to
    #: ``None`` so existing adopters are unaffected, but a named sentinel such
    #: as ``ReaperAction.NOOP`` is arguably the better design: it is countable,
    #: which is how a sweep reports how often it found nothing to do. Without
    #: this member the profile would fail such an implementation on vocabulary
    #: rather than behaviour — identically for any implementation that names its
    #: dispositions. Profile C already declares its dispositions explicitly and
    #: ``_is_refused`` already accepts two error styles; this member is what
    #: makes profile B consistent with both.
    no_disposition: Any = None

    #: Give a claimed row back unchanged, under its token — no retry budget
    #: spent, no error recorded. Optional: not every design has a release that
    #: is distinct from lease expiry. When supplied it is proven to leave the
    #: row due again with its budget intact, which is the property that
    #: distinguishes it (a fleet drained during a provider incident must not
    #: burn budget on rows that were never tried).
    release_unchanged: Callable[[Any, UUID], bool] | None = None
    #: Optional. A comparable summary of the owner-visible state that the fenced
    #: operations change — for a chunk, its state plus its lease deadline.
    #: Same shape as :func:`~.eventual_convergence.assert_superseded_snapshot_does_not_write`
    #: takes, deliberately.
    #:
    #: Supplying it upgrades all four token proofs from *the call was not
    #: refused* to *the call did what it claimed*:
    #:
    #: * an accepted write must change the observed state, so an implementation
    #:   that returns success and writes nothing is caught;
    #: * a refused write must leave it identical, so one that reports refusal
    #:   after already writing is caught.
    #:
    #: Optional because it is not always available: the observation is per-domain
    #: and some adopters have no cheap way to summarise the row. A shared
    #: work-table library, which owns the row shape, generally does — and gets
    #: the stronger contract for one lambda.
    observe: Callable[[Any], Any] | None = None


def assert_ownership_transitions_are_production_bound(ownership: FencedOwnership) -> None:
    """INVARIANT 0: ownership callbacks invoke production transitions."""
    bindings = (
        ("claim", ownership.claim, "the production claim transition"),
        ("fenced_write", ownership.fenced_write, "the production fenced write"),
        ("renew_lease", ownership.renew_lease, "the production lease renewal"),
        ("reclaim_stalled", ownership.reclaim_stalled, "the production stalled-work recovery transition"),
    )
    if ownership.release_unchanged is not None:
        bindings = (
            *bindings,
            ("release_unchanged", ownership.release_unchanged, "the production unchanged-release transition"),
        )
    for field, binding, shape in bindings:
        assert_test_binding_forwards(
            adopter=ownership.name,
            field=field,
            binding=binding,
            forbidden=INVOCATION_AUTHORING_OPERATIONS,
            production_shape=shape,
        )


def _observed(ownership: FencedOwnership, row_id: Any) -> Any:
    """The row's owner-visible state, or a sentinel when unobservable."""
    return None if ownership.observe is None else ownership.observe(row_id)


def _assert_untouched(ownership: FencedOwnership, row_id: Any, before: Any, what: str) -> None:
    """
    A refused operation must also have changed nothing.

    No-op when the adopter supplies no ``observe``: the return value is then the
    only signal available, and this proof degrades to what it always was.
    """
    if ownership.observe is None:
        return
    after = ownership.observe(row_id)
    assert after == before, (
        f"{ownership.name}: {what} was correctly refused, but the observed state "
        f"moved from {before!r} to {after!r}. A guard that reports failure after "
        f"the write has already landed refuses nothing"
    )


def _claim_one(ownership: FencedOwnership) -> tuple[Any, UUID]:
    ownership.make_claimable()
    claimed = ownership.claim()
    assert claimed is not None, (
        f"{ownership.name}: a row was made claimable but claim() returned None; "
        f"the rest of this profile cannot be evaluated"
    )
    return claimed


def _is_refused(result: object) -> bool:
    """
    A fenced write may signal refusal by returning False or by raising.

    Both are legitimate designs, so the proofs accept either rather than forcing
    a domain to change its error style to satisfy the contract.

    ``None`` is deliberately NOT a refusal. It is what a Python function returns
    when it has no explicit ``return``, so accepting it would mean a binding that
    silently does nothing reports refusal for every input — and the refusal
    proofs would pass against an implementation that never applies anything
    either. A domain that genuinely signals refusal does so deliberately, with
    ``False`` or an exception.
    """
    return result is False


#: The semantic bindings of profile B: (field, whether None is permitted).
_OWNERSHIP_SEMANTIC_FIELDS: tuple[tuple[str, bool], ...] = (
    ("claim", False),
    ("fenced_write", False),
    ("renew_lease", False),
    ("reclaim_stalled", False),
    ("release_unchanged", True),
)


def assert_ownership_bindings_are_production_bound(ownership: FencedOwnership) -> None:
    """
    INVARIANT 0, delegation half: the claim/fence/lease/reap surface REACHES
    runtime code.

    The authorship half lives in
    :func:`assert_ownership_transitions_are_production_bound`, which runs with
    the behavioral tuple and rejects ORM re-implementations. What it cannot
    reject is profile B's cheapest counterfeit: a dict-based in-memory owner —
    exactly what this package's own self-tests bind as the conforming
    reference — which authors no ORM at all and passes every behavioral proof
    here while production does none of it. This delegation check runs at the
    contract layer and in the composite, where the raw tuple's mutation
    catalogs are not in play. ``make_claimable`` is arrange, ``expire_lease``
    is fault injection, ``observe`` is an observer — all deliberately
    unchecked, per the shared boundary rule.
    """
    for field_name, optional in _OWNERSHIP_SEMANTIC_FIELDS:
        binding = getattr(ownership, field_name)
        if binding is None and optional:
            continue
        assert_test_binding_delegates_to_production(
            adopter=ownership.name,
            field=field_name,
            binding=binding,
            production_shape=f"the production {field_name.replace('_', ' ')} operation",
        )


def assert_claim_is_exclusive(ownership: FencedOwnership) -> None:
    """INVARIANT 1: two claims cannot both take the same row."""
    first = _claim_one(ownership)
    second = ownership.claim()
    assert second is None or second[0] != first[0], (
        f"{ownership.name}: the same row was claimed twice "
        f"({first[0]!r}), so two workers would execute the same effect "
        f"concurrently"
    )


def assert_fence_rotates_through_recovery(ownership: FencedOwnership) -> None:
    """
    INVARIANT 2: reclaiming a dead owner's row issues a new token.

    Exercised through the production death-recovery path — claim, expire the
    lease, reclaim, claim again — rather than through ``release``. Release is
    not an operation in every design, so requiring it would force adapters to
    invent one purely to satisfy this proof. Recovery is the sequence every
    implementation must already support, which makes it the portable one.
    """
    row_id, first_token = _claim_one(ownership)
    ownership.expire_lease(row_id)
    ownership.reclaim_stalled(row_id)

    reclaimed = ownership.claim()
    assert reclaimed is not None, (
        f"{ownership.name}: a row reclaimed after lease expiry was not claimable again, so a dead owner strands it"
    )
    assert reclaimed[1] != first_token, (
        f"{ownership.name}: re-claiming after recovery reissued the same token "
        f"{first_token!r}. The dead owner's token is therefore still valid, so "
        f"if it wakes up its write is accepted"
    )


def assert_release_returns_the_row_unspent(ownership: FencedOwnership) -> None:
    """
    OPTIONAL: stepping aside is not the same as failing.

    Only meaningful for designs where release exists as its own operation. A
    kill switch, a provider-wide block, or a graceful shutdown makes a worker
    hand a row back for reasons that say nothing about that row's prospects; if
    that path spends retry budget, a fleet drained during a provider incident
    turns a recoverable backlog into an exhausted one.

    Adopters without a distinct release decline this proof by leaving
    ``release_unchanged`` unset, rather than implementing one for the test.
    """
    if ownership.release_unchanged is None:
        return

    row_id, token = _claim_one(ownership)
    assert ownership.release_unchanged(row_id, token), (
        f"{ownership.name}: release_unchanged refused a row its caller had just claimed under the current token"
    )
    assert ownership.claim() is not None, (
        f"{ownership.name}: a released row was not claimable again, so stepping aside stranded it"
    )


def assert_current_token_write_is_accepted(ownership: FencedOwnership) -> None:
    """
    INVARIANT 3a: the fence lets the RIGHTFUL owner through.

    The complement of invariants 3 and 6, and the reason they mean anything.
    Both of those assert only that a *superseded* token is refused, which an
    implementation satisfies by refusing everything — or by doing nothing at
    all, since a binding with no explicit ``return`` used to read as a refusal.
    Measured before this proof existed: an in-memory owner whose ``fenced_write``
    and ``renew_lease`` were ``lambda row, token: None`` passed all six profile B
    invariants.

    This is the same pairing profile C makes explicit for ambiguity — "either
    alone is satisfiable by a degenerate implementation that always picks one
    branch" — and that profile E makes with
    :func:`~.eventual_convergence.assert_unsettled_state_converges`. Rejecting stale
    writes must not cost you the current ones.

    Without ``observe`` this is a weak positive: it asserts the write was not
    *refused*, not that it had an effect. Requiring a truthy return instead would
    outlaw the legitimate design that raises on refusal and returns nothing on
    success, so the base form stays permissive and takes its force from the
    pairing — the no-op that passes it is caught by invariants 3 and 6, which no
    longer read ``None`` as a refusal.

    With ``observe`` it becomes a real positive: the write must change the
    observed state. An adopter that owns its row shape — a shared work-table library especially —
    gets that for one lambda.
    """
    row_id, token = _claim_one(ownership)
    before = _observed(ownership, row_id)

    result = ownership.fenced_write(row_id, token)
    assert not _is_refused(result), (
        f"{ownership.name}: a write carrying the row's CURRENT token was "
        f"refused (returned {result!r}). Invariants 3 and 6 only prove a stale "
        f"token is rejected, so an implementation that refuses everything — or "
        f"does nothing — satisfies them both. The fence has to let the rightful "
        f"owner through"
    )
    if ownership.observe is None:
        return
    after = ownership.observe(row_id)
    assert after != before, (
        f"{ownership.name}: the write reported success under the current token "
        f"but left the observed state at {before!r}. Reporting an effect that did "
        f"not happen is worse than refusing it: the caller proceeds as though the "
        f"row moved on"
    )


def assert_current_token_renewal_is_accepted(ownership: FencedOwnership) -> None:
    """
    INVARIANT 6a: the lease heartbeat works for the owner holding the lease.

    Separate from :func:`assert_current_token_write_is_accepted` for the same
    reason invariants 3 and 6 are separate: renewal and the owner-gated write are
    different code paths, and an implementation can get one right and the other
    wrong. Without this, invariant 6 is satisfied by a heartbeat that never
    renews anything, which strands the owner exactly as surely as one that
    accepts a stale token.

    The lease is expired first, so ``observe`` sees a deadline that is
    unambiguously in the past and renewal has to move it. Comparing two live
    deadlines instead would rest on clock resolution; this rests on the sign of
    the change. Renewing an expired-but-unreclaimed lease is the owner that is
    slow rather than dead, which is exactly who a heartbeat is for.
    """
    row_id, token = _claim_one(ownership)
    ownership.expire_lease(row_id)
    before = _observed(ownership, row_id)

    result = ownership.renew_lease(row_id, token)
    assert not _is_refused(result), (
        f"{ownership.name}: renewing the lease under the row's CURRENT token was "
        f"refused (returned {result!r}). A heartbeat that never succeeds lets the "
        f"reaper take the row from a worker that is still running it"
    )
    if ownership.observe is None:
        return
    after = ownership.observe(row_id)
    assert after != before, (
        f"{ownership.name}: renewal reported success but the observed state is "
        f"still {before!r}, so the deadline never moved. The reaper will take the "
        f"row from a worker that believes it holds the lease"
    )


def assert_stale_token_is_rejected(ownership: FencedOwnership) -> None:
    """
    INVARIANT 3: a superseded owner cannot write.

    With ``observe`` this also asserts the row is untouched. Refusing and
    writing anyway is a real shape — a guard that reports failure after the
    update, or one that checks the fence on the wrong side of the write — and a
    return value alone cannot see it.
    """
    row_id, stale_token = _claim_one(ownership)
    ownership.expire_lease(row_id)
    ownership.reclaim_stalled(row_id)
    assert ownership.claim() is not None, f"{ownership.name}: re-claim failed"
    before = _observed(ownership, row_id)

    try:
        result: object = ownership.fenced_write(row_id, stale_token)
    except Exception:
        _assert_untouched(ownership, row_id, before, "the refused write")
        return  # raising is a legitimate refusal
    assert _is_refused(result), (
        f"{ownership.name}: a write carrying the superseded token "
        f"{stale_token!r} was accepted. A slow worker that wakes up after its "
        f"claim was reaped would corrupt the new owner's state"
    )
    _assert_untouched(ownership, row_id, before, "the refused write")


def assert_live_lease_blocks_reclaim(ownership: FencedOwnership) -> None:
    """INVARIANT 4: the reaper does not take a row from a live owner."""
    row_id, _ = _claim_one(ownership)
    action = ownership.reclaim_stalled(row_id)
    assert action == ownership.no_disposition, (
        f"{ownership.name}: the reaper disposed of a row whose lease is still "
        f"live (action={action!r}, expected {ownership.no_disposition!r}), so the "
        f"current owner and the reclaimer would both execute the effect"
    )


def assert_expired_lease_permits_reclaim(ownership: FencedOwnership) -> None:
    """INVARIANT 5: a dead owner's row is recoverable."""
    row_id, _ = _claim_one(ownership)
    ownership.expire_lease(row_id)
    action = ownership.reclaim_stalled(row_id)
    assert action != ownership.no_disposition, (
        f"{ownership.name}: a row whose lease expired was not reclaimable "
        f"(the reaper returned its did-nothing value {action!r}), so an owner "
        f"that died mid-effect strands it permanently"
    )


def assert_lease_renewal_is_fenced(ownership: FencedOwnership) -> None:
    """
    INVARIANT 6: a dead owner cannot keep its lease alive.

    With ``observe`` this also asserts the deadline did not move, which is the
    thing that actually matters: a renewal that reports refusal and extends the
    lease anyway keeps the row away from the reaper just as effectively as one
    that reports success.
    """
    row_id, stale_token = _claim_one(ownership)
    ownership.expire_lease(row_id)
    ownership.reclaim_stalled(row_id)
    assert ownership.claim() is not None, f"{ownership.name}: re-claim failed"
    before = _observed(ownership, row_id)

    try:
        result: object = ownership.renew_lease(row_id, stale_token)
    except Exception:
        _assert_untouched(ownership, row_id, before, "the refused renewal")
        return
    assert _is_refused(result), (
        f"{ownership.name}: lease renewal accepted the superseded token "
        f"{stale_token!r}, so a dead owner could hold the lease open and block "
        f"reclaim indefinitely"
    )
    _assert_untouched(ownership, row_id, before, "the refused renewal")


def assert_the_lease_outlives_the_work(
    *,
    name: str,
    lease: "timedelta",
    max_work_duration: "timedelta",
    renews_during_work: bool,
) -> None:
    """
    A worker must not lose its lease while still working.

    The ordinary operational events — a slow provider, a throttled round, a
    worker that simply has a lot to do — all stretch execution. If the lease
    expires first, the reaper reclaims a row whose original worker is still
    running it, and the effect executes twice. Nothing has crashed and no code
    is wrong; the two durations were simply set independently.

    Two ways to be safe, and an implementation needs exactly one: make the lease
    longer than the worst-case run, or renew it during the run. A throttled
    batch sender — an email campaign claiming chunks, say — typically takes the
    second route, renewing each throttle round, which is why a lease far shorter
    than the worker's hard time limit is correct there.
    """
    if renews_during_work:
        return
    assert lease > max_work_duration, (
        f"{name}: the lease is {lease} but work can run for {max_work_duration} "
        f"and is never renewed, so the reaper reclaims rows from workers that "
        f"are still running them. Either extend the lease past the worst-case "
        f"run or renew it during the run"
    )


def assert_claim_is_exclusive_across_connections(ownership: FencedOwnership, *, timeout: float = 5.0) -> None:
    """
    INVARIANT 1, proven properly: two real connections, one winner.

    :func:`assert_claim_is_exclusive` claims twice in sequence, which catches a
    missing ``WHERE state = READY`` but cannot catch a claim that is not
    atomic — two workers reading before either writes. Only genuine
    concurrency shows that, and an earlier revision of this module recorded
    two-connection races as something a portable contract "structurally cannot
    do". That was wrong — this proof does exactly that — and the claim is
    withdrawn.

    The race pattern: one thread per racer, each inside the host's
    ``connection_scope()`` so it runs on its own database connection and
    releases it on exit, a barrier to force the overlap, and exceptions
    collected rather than swallowed.

    The test must run with real commits — see the host's
    ``database_marks(True)``; inside a test-wrapping transaction the racer
    threads cannot see the row.
    """
    ownership.make_claimable()

    barrier = threading.Barrier(2, timeout=timeout)
    claims: list[tuple[Any, UUID] | None] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    connection_scope = current_host().connection_scope

    def racer() -> None:
        with connection_scope():
            try:
                barrier.wait()
                claimed = ownership.claim()
                with lock:
                    claims.append(claimed)
            except BaseException as error:  # noqa: BLE001 - reported, never swallowed
                with lock:
                    errors.append(error)

    threads = [threading.Thread(target=racer, name=f"claim-racer-{i}") for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout * 2)

    assert not errors, (
        f"{ownership.name}: a racing claim raised instead of losing cleanly: "
        f"{errors!r}. Losing a claim race is an ordinary outcome and must "
        f"return None, not error"
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1, (
        f"{ownership.name}: {len(winners)} of 2 concurrent claims won "
        f"({winners!r}). The claim is not atomic, so two workers would execute "
        f"the same effect at the same time"
    )


FENCED_OWNERSHIP_PROOFS: tuple[Callable[[FencedOwnership], None], ...] = (
    assert_ownership_transitions_are_production_bound,
    assert_claim_is_exclusive,
    assert_fence_rotates_through_recovery,
    # The complements run beside the refusal proofs they make meaningful.
    assert_current_token_write_is_accepted,
    assert_stale_token_is_rejected,
    assert_current_token_renewal_is_accepted,
    assert_live_lease_blocks_reclaim,
    assert_expired_lease_permits_reclaim,
    assert_lease_renewal_is_fenced,
    assert_release_returns_the_row_unspent,
)


def assert_fenced_ownership_contract(ownership: FencedOwnership) -> None:
    assert_ownership_bindings_are_production_bound(ownership)
    for proof in FENCED_OWNERSHIP_PROOFS:
        proof(ownership)
