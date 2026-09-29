"""
The fenced-ownership proofs (profile B), pointed at implementations we control.

A proof that no implementation can fail measures nothing, and a proof that
fails the wrong implementations is worse. So every proof in
`due_work_harness.profiles.bounded_ownership` is run here in both
directions: a minimal in-memory owner that conforms passes all of them, and a
family of deliberately broken owners each fails *exactly* the invariant it
breaks and no other.

The in-memory owner stands in for a domain's ownership mechanics — the seam it
replaces is the adopter's `FencedOwnership` binding, not any real application model.
It exists because the discrimination question ("does this proof catch the
defect it hunts, and only that one?") is a property of the proofs, so it must
be answerable without a database and without borrowing some domain's tables.
The DB-coupled proof `assert_claim_is_exclusive_across_connections`, whose broken
direction is a genuine data race, is run here against a locked claim (passes),
a claim made non-atomic by a deterministic rendezvous, a racer that never
returns, and a racer that raises (each fails, with its own message). Real row
locking gets its coverage from adopters.
"""

import threading
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from due_work_harness.host import race_timeout
from due_work_harness.profiles.bounded_ownership import (
    FENCED_OWNERSHIP_PROOFS,
    FencedOwnership,
    assert_claim_is_exclusive,
    assert_claim_is_exclusive_across_connections,
    assert_current_token_renewal_is_accepted,
    assert_current_token_write_is_accepted,
    assert_expired_lease_permits_reclaim,
    assert_fence_rotates_through_recovery,
    assert_fenced_ownership_contract,
    assert_lease_renewal_is_fenced,
    assert_live_lease_blocks_reclaim,
    assert_ownership_bindings_are_production_bound,
    assert_ownership_transitions_are_production_bound,
    assert_release_returns_the_row_unspent,
    assert_stale_token_is_rejected,
    assert_the_lease_outlives_the_work,
)
from due_work_harness.references.in_memory import (
    REFERENCE_LEASE_SECONDS as _LEASE_SECONDS,
)
from due_work_harness.references.in_memory import (
    InMemoryOwner as _InMemoryOwner,
)

# The conforming owner lives in harness.due_work.references — root-owned, because the
# binding guard rejects a test-module implementation that reaches no
# production code, and rejecting it is exactly what this file's
# test_the_binding_guard_rejects_a_test_module_owner pins. The broken variants
# stay here: they run against individual behavioral proofs, where no binding
# guard is involved.


def _binding(owner: _InMemoryOwner, *, observe: bool = True) -> FencedOwnership:
    return FencedOwnership(
        name="in-memory reference owner",
        make_claimable=owner.make_claimable,
        claim=owner.claim,
        fenced_write=owner.fenced_write,
        other_fenced_writes={"retry": owner.fenced_retry},
        renew_lease=owner.renew_lease,
        expire_lease=owner.expire_lease,
        reclaim_stalled=owner.reclaim_stalled,
        release_unchanged=owner.release_unchanged,
        observe=owner.observe if observe else None,
    )


class _DoubleClaims(_InMemoryOwner):
    """Breaks invariant 1: the claim ignores state, so a row is claimed twice."""

    def claim(self) -> tuple[int, UUID] | None:
        for row_id, row in sorted(self.rows.items()):
            row.state = "CLAIMED"
            row.token = self._issue_token()
            row.lease_expires_at = self.clock + _LEASE_SECONDS
            return (row_id, row.token)
        return None


class _ReusesTokens(_InMemoryOwner):
    """
    Breaks invariant 2: the fence never rotates.

    Expected to take the stale-token proofs down with it: when re-claiming
    issues the same token, a superseded owner's token *is* the current one, so
    stale writes and stale renewals are structurally indistinguishable from
    legitimate ones. That knock-on is the whole argument for rotation.
    """

    _FIXED = uuid4()

    def _issue_token(self) -> UUID:
        return self._FIXED


class _RefusesEverything(_InMemoryOwner):
    """Breaks 3a/6a: the fence never lets even the rightful owner through."""

    def fenced_write(self, row_id: int, token: UUID) -> bool:
        return False

    def renew_lease(self, row_id: int, token: UUID) -> bool:
        return False


class _DoesNothing(_InMemoryOwner):
    """
    The degenerate that once passed the whole profile: bindings return None.

    ``None`` used to read as a refusal, so an owner whose fenced operations did
    nothing at all satisfied every invariant. It must now fail the stale-token
    proofs (None is not a refusal) and, with ``observe``, the acceptance proofs
    too (nothing changed).
    """

    def fenced_write(self, row_id: int, token: UUID):  # type: ignore[override]
        return None

    def renew_lease(self, row_id: int, token: UUID):  # type: ignore[override]
        return None


class _ReportsSuccessWritesNothing(_InMemoryOwner):
    """Breaks 3a/6a only under ``observe``: success is reported, no effect lands."""

    def fenced_write(self, row_id: int, token: UUID) -> bool:
        return token == self.rows[row_id].token

    def renew_lease(self, row_id: int, token: UUID) -> bool:
        return token == self.rows[row_id].token


class _RefusesAfterWriting(_InMemoryOwner):
    """Breaks 3/6 under ``observe``: the guard reports failure after the write."""

    def fenced_write(self, row_id: int, token: UUID) -> bool:
        row = self.rows[row_id]
        row.state = "RETRYABLE"
        row.lease_expires_at = None
        return token == row.token

    def renew_lease(self, row_id: int, token: UUID) -> bool:
        row = self.rows[row_id]
        # Extend from the current deadline rather than the (never-advancing)
        # simulated clock, so the premature write is observable.
        row.lease_expires_at = (row.lease_expires_at or self.clock) + _LEASE_SECONDS
        return token == row.token


class _AcceptsStaleTokens(_InMemoryOwner):
    """Breaks invariants 3 and 6: the fence is decorative."""

    def fenced_write(self, row_id: int, token: UUID) -> bool:
        row = self.rows[row_id]
        row.state = "RETRYABLE"
        row.lease_expires_at = None
        return True

    def renew_lease(self, row_id: int, token: UUID) -> bool:
        self.rows[row_id].lease_expires_at = self.clock + _LEASE_SECONDS
        return True


class _FencesOnlyTheFinish(_InMemoryOwner):
    """Breaks invariant 3 for its second write only: a stale owner can still send the row back."""

    def fenced_retry(self, row_id: int, token: UUID) -> bool:
        row = self.rows[row_id]
        row.state = "READY"
        row.lease_expires_at = None
        return True


class _ReaperIgnoresTheLease(_InMemoryOwner):
    """Breaks invariant 4: a live owner's row is taken from under it."""

    def reclaim_stalled(self, row_id: int) -> str | None:
        self.rows[row_id].state = "READY"
        return "REARMED"


class _ReaperNeverReclaims(_InMemoryOwner):
    """Breaks invariant 5: a dead owner strands its row forever."""

    def reclaim_stalled(self, row_id: int) -> str | None:
        return None


class _ReleaseStrandsTheRow(_InMemoryOwner):
    """Breaks the release proof: stepping aside does not make the row claimable."""

    def release_unchanged(self, row_id: int, token: UUID) -> bool:
        return token == self.rows[row_id].token


def _verdicts(make_owner: Callable[[], _InMemoryOwner], *, observe: bool = True) -> set[str]:
    """The names of the proofs a fresh instance of ``make_owner`` fails."""
    failed: set[str] = set()
    for proof in FENCED_OWNERSHIP_PROOFS:
        try:
            proof(_binding(make_owner(), observe=observe))
        except AssertionError:
            failed.add(proof.__name__)
    return failed


def test_the_conforming_owner_passes_every_proof() -> None:
    assert_fenced_ownership_contract(_binding(_InMemoryOwner()))


def test_the_conforming_owner_passes_without_observe_too() -> None:
    """``observe`` is optional; its absence must not turn passes into failures."""
    assert_fenced_ownership_contract(_binding(_InMemoryOwner(), observe=False))


@pytest.mark.parametrize(
    ("make_owner", "expected_failures"),
    [
        pytest.param(
            _DoubleClaims,
            {assert_claim_is_exclusive.__name__},
            id="double-claim",
        ),
        pytest.param(
            _ReusesTokens,
            {
                assert_fence_rotates_through_recovery.__name__,
                # A non-rotating fence cannot reject stale writers: the stale
                # token is still the current one. See the class docstring.
                assert_stale_token_is_rejected.__name__,
                assert_lease_renewal_is_fenced.__name__,
            },
            id="fence-never-rotates",
        ),
        pytest.param(
            _RefusesEverything,
            {
                assert_current_token_write_is_accepted.__name__,
                assert_current_token_renewal_is_accepted.__name__,
            },
            id="refuses-everything",
        ),
        pytest.param(
            _AcceptsStaleTokens,
            {
                assert_stale_token_is_rejected.__name__,
                assert_lease_renewal_is_fenced.__name__,
            },
            id="fence-is-decorative",
        ),
        pytest.param(
            _FencesOnlyTheFinish,
            {assert_stale_token_is_rejected.__name__},
            id="fence-guards-only-the-finish",
        ),
        pytest.param(
            _ReaperIgnoresTheLease,
            {assert_live_lease_blocks_reclaim.__name__},
            id="reaper-ignores-live-lease",
        ),
        pytest.param(
            _ReaperNeverReclaims,
            {
                assert_expired_lease_permits_reclaim.__name__,
                # With no reclaim, recovery cannot rotate the fence either.
                assert_fence_rotates_through_recovery.__name__,
                # The stale-token proofs cannot even set up their re-claim.
                assert_stale_token_is_rejected.__name__,
                assert_lease_renewal_is_fenced.__name__,
            },
            id="reaper-never-reclaims",
        ),
        pytest.param(
            _ReleaseStrandsTheRow,
            {assert_release_returns_the_row_unspent.__name__},
            id="release-strands-the-row",
        ),
    ],
)
def test_each_broken_owner_fails_exactly_its_own_proofs(
    make_owner: Callable[[], _InMemoryOwner], expected_failures: set[str]
) -> None:
    assert _verdicts(make_owner) == expected_failures


def test_a_do_nothing_owner_no_longer_passes_the_profile() -> None:
    """
    Regression pin for the measured degenerate in the module docstring.

    An owner whose fenced operations return ``None`` once passed every
    invariant, because ``None`` read as a refusal. It must fail the stale-token
    proofs now that only ``False`` (or raising) counts as refusing — and with
    ``observe`` it fails the acceptance proofs as well.
    """
    with_observe = _verdicts(_DoesNothing)
    assert with_observe == {
        assert_current_token_write_is_accepted.__name__,
        assert_current_token_renewal_is_accepted.__name__,
        assert_stale_token_is_rejected.__name__,
        assert_lease_renewal_is_fenced.__name__,
    }
    without_observe = _verdicts(_DoesNothing, observe=False)
    assert without_observe == {
        assert_stale_token_is_rejected.__name__,
        assert_lease_renewal_is_fenced.__name__,
    }


def test_observe_is_what_catches_a_liar() -> None:
    """
    The upgrade ``observe`` buys, measured rather than described.

    An owner that refuses stale tokens correctly but writes nothing on success
    passes the whole profile without ``observe`` and fails the two acceptance
    proofs with it. An owner that refuses correctly but has already written
    fails the two refusal proofs only under ``observe``.
    """
    liar = _ReportsSuccessWritesNothing
    assert _verdicts(liar, observe=False) == set()
    assert _verdicts(liar) == {
        assert_current_token_write_is_accepted.__name__,
        assert_current_token_renewal_is_accepted.__name__,
    }

    late_guard = _RefusesAfterWriting
    assert _verdicts(late_guard, observe=False) == set()
    assert _verdicts(late_guard) == {
        assert_stale_token_is_rejected.__name__,
        assert_lease_renewal_is_fenced.__name__,
    }


def test_an_owner_without_release_declines_that_proof() -> None:
    """``release_unchanged=None`` is a decline, not a failure."""
    owner = _InMemoryOwner()
    binding = FencedOwnership(
        name="no-release owner",
        make_claimable=owner.make_claimable,
        claim=owner.claim,
        fenced_write=owner.fenced_write,
        renew_lease=owner.renew_lease,
        expire_lease=owner.expire_lease,
        reclaim_stalled=owner.reclaim_stalled,
        release_unchanged=None,
    )
    assert_release_returns_the_row_unspent(binding)


class _LockedClaims(_InMemoryOwner):
    """
    The conforming owner with an atomic claim, for the race proof.

    The dict-based claim above is check-then-set, which is exactly the shape
    the two-connection proof exists to catch — under two threads it can
    genuinely double-claim, nondeterministically. A lock is this owner's
    equivalent of the database's atomicity, and the nondeterministic failing
    direction is deliberately not asserted here: a test that fails only when a
    race happens to interleave is a flake, not a proof. Adopters run this proof
    against real row locking.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def claim(self) -> tuple[int, UUID] | None:
        with self._lock:
            return super().claim()


def test_the_race_proof_passes_a_locked_claim() -> None:
    assert_claim_is_exclusive_across_connections(_binding(_LockedClaims()))


class _RendezvousClaims(_InMemoryOwner):
    """
    A non-atomic claim made deterministic: both racers read READY before either writes.

    The plain dict claim double-claims only when the threads happen to interleave,
    which would make a flaky test. Here each racer waits, between its read and its
    write, until the other has read too, so the double claim always happens.
    """

    def __init__(self) -> None:
        super().__init__()
        self._both_have_read = threading.Barrier(2, timeout=5)

    def claim(self) -> tuple[int, UUID] | None:
        for row_id, row in sorted(self.rows.items()):
            if row.state == "READY":
                self._both_have_read.wait()
                row.state = "CLAIMED"
                row.token = uuid4()
                row.lease_expires_at = self.clock + _LEASE_SECONDS
                return (row_id, row.token)
        return None


def test_the_race_proof_fails_a_claim_that_is_not_atomic() -> None:
    with pytest.raises(AssertionError, match="2 of 2 concurrent claims won"):
        assert_claim_is_exclusive_across_connections(_binding(_RendezvousClaims()))


class _HungClaims(_LockedClaims):
    """
    An atomic claim whose loser never comes back: it waits for a lock nothing releases.

    The winner returns, so counting only the racers that returned finds exactly one
    winner. Blocked on the other connection's uncommitted claim, the loser is what a
    deadlock or a stalled claim looks like.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self._first = threading.Lock()
        self._entered = False

    def claim(self) -> tuple[int, UUID] | None:
        with self._first:
            first, self._entered = not self._entered, True
        if not first:
            self.release.wait(timeout=30)
        return super().claim()


def test_the_race_proof_fails_when_a_racer_never_returns() -> None:
    owner = _HungClaims()
    try:
        with pytest.raises(
            AssertionError, match=r"claim-racer-\d had not returned 0\.3s after the race began .* blocked, not lost"
        ):
            assert_claim_is_exclusive_across_connections(_binding(owner), timeout=0.3)
    finally:
        owner.release.set()


def test_a_racer_that_raises_fails_the_race_proof_with_its_error() -> None:
    class _Raises(_LockedClaims):
        def claim(self) -> tuple[int, UUID] | None:
            raise RuntimeError("deadlock detected")

    with pytest.raises(AssertionError, match="raised instead of losing cleanly.*deadlock detected"):
        assert_claim_is_exclusive_across_connections(_binding(_Raises()))


def test_the_binding_guard_rejects_a_test_module_owner() -> None:
    """
    Profile B's counterfeit is this very file's reference owner: a dict machine
    that passes every behavioral proof while production does none of it. The
    binding guard accepts the root-owned reference (the composite above runs
    it) and must reject the same shape the moment it is authored in a test
    module — this class is a byte-for-byte re-implementation of the claim.
    """

    class _TestModuleOwner(_InMemoryOwner):
        def claim(self) -> tuple[int, UUID] | None:
            for row_id, row in sorted(self.rows.items()):
                if row.state == "READY":
                    row.state = "CLAIMED"
                    row.token = uuid4()
                    row.lease_expires_at = self.clock + _LEASE_SECONDS
                    return (row_id, row.token)
            return None

    with pytest.raises(AssertionError, match="claim.*references no production"):
        assert_ownership_bindings_are_production_bound(_binding(_TestModuleOwner()))


def test_the_binding_guard_rejects_an_orm_authored_fence() -> None:
    """A fenced write copied into the adapter as ORM code is authorship, not forwarding."""

    class _FakeManager(list):
        def filter(self, **kwargs) -> "_FakeManager":
            return self

        def update(self, **kwargs) -> int:
            return 1

    class _FakeModel:
        objects = _FakeManager()

    owner = _InMemoryOwner()
    binding = FencedOwnership(
        name="orm-authored owner",
        make_claimable=owner.make_claimable,
        claim=owner.claim,
        fenced_write=lambda row_id, token: bool(_FakeModel.objects.filter(pk=row_id, token=token).update(state="X")),
        renew_lease=owner.renew_lease,
        expire_lease=owner.expire_lease,
        reclaim_stalled=owner.reclaim_stalled,
    )
    with pytest.raises(AssertionError, match="fenced_write.*authors production semantics"):
        assert_ownership_transitions_are_production_bound(binding)


def test_the_lease_duration_proof_reads_the_configuration() -> None:
    from datetime import timedelta

    # Renewing during work discharges the duration requirement entirely.
    assert_the_lease_outlives_the_work(
        name="renewing worker",
        lease=timedelta(seconds=30),
        max_work_duration=timedelta(hours=1),
        renews_during_work=True,
    )
    # Without renewal the lease must exceed the worst-case run.
    with pytest.raises(AssertionError, match="never renewed"):
        assert_the_lease_outlives_the_work(
            name="non-renewing worker",
            lease=timedelta(seconds=30),
            max_work_duration=timedelta(hours=1),
            renews_during_work=False,
        )


def test_each_racer_knows_the_race_deadline_so_the_host_can_bound_its_waits() -> None:
    seen: list[float | None] = []

    class _Recording(_LockedClaims):
        def claim(self) -> tuple[int, UUID] | None:
            seen.append(race_timeout())
            return super().claim()

    assert_claim_is_exclusive_across_connections(_binding(_Recording()), timeout=3)
    assert seen == [3, 3]
    assert race_timeout() is None, "outside the race the calling thread is no racer"
