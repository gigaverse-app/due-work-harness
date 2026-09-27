"""
Profile D of the due-work harness: durable retention.

Retention and durability are independent — a row can be durable while owed and
deliberately removed once settled — but the boundary is easy to get wrong in
the dangerous direction. A TTL or pruning pass keyed on age rather than on
terminal state will eventually delete a row that is still claimed or ambiguous,
which destroys the only record that the work exists.

Two invariants, deliberately paired: everything else about retention (what to
keep, for how long, where it goes) is domain policy; "never delete work that is
still owed" is the part every policy must share, and "the pass really prunes
what the policy says is prunable" is the positive control that keeps the first
one from being satisfiable by a retention pass that deletes nothing — or by a
``run_retention`` binding that never reaches the real pass at all. The same
pairing discipline as profile B's 3a/6a and profile F's invariants 1 and 4.

A note on how this profile goes dead, because the mechanism repeats. Asked as
"does anything prune or delete *work* rows?" — phrased as something another
process might do to your rows — the question is easy to answer "no" in good
faith. A codebase can accumulate contract after contract declaring
:class:`~.contract.NotApplicable` with a variant of "no retention pass exists",
none of them wrong about its own tables, while a scheduled cleanup job deletes
user accounts every day. The cleanup is invisible to the question because
deletion is not something that happens *to* its work — deletion **is** its
work, so nobody asking "does anything prune my work rows?" ever reaches this
profile. Shared guidance that answers the question on an adopter's behalf ("no
sweep here prunes terminal work") is the documentation form of a sham
contract: an assertion of fact in shared guidance is copied, not checked.

So the question this profile actually asks is: **does this domain delete rows
on a schedule — including when deleting is the point?** An account-cleanup job
is the canonical claimant, and typically the highest-stakes deletion in a
system: a wrong prune destroys a real user along with every identity mirrored
into external providers.
"""

from collections.abc import Callable
from typing import Any

from due_work_harness.binding import (
    INVOCATION_AUTHORING_OPERATIONS,
    assert_binding_reaches_production,
)
from due_work_harness.models import HarnessModel


class Retention(HarnessModel):
    """One domain's retention pass, described so the proofs can drive it."""

    name: str

    #: Create one row that is owed or in flight — anything but terminal.
    make_non_terminal: Callable[[], Any]

    #: Create one row the domain's own policy says is prunable — terminal, and
    #: past whatever retention window applies. The positive control needs it:
    #: without a row that must go, "nothing wrongly deleted" is satisfiable by
    #: a pass that deletes nothing.
    make_prunable: Callable[[], Any]

    #: Run one retention/pruning pass, exactly as production schedules it.
    run_retention: Callable[[], Any]

    #: Whether the row created by either factory still exists.
    still_exists: Callable[[Any], bool]


def assert_retention_transition_is_production_bound(retention: Retention) -> None:
    """PROFILE D0: the pruning pass is production code, not a test copy or fake."""
    assert_binding_reaches_production(
        adopter=retention.name,
        field="run_retention",
        binding=retention.run_retention,
        forbidden=INVOCATION_AUTHORING_OPERATIONS,
        production_shape="the production retention pass",
    )


def assert_retention_preserves_non_terminal_work(retention: Retention) -> None:
    """PROFILE D: retention never deletes work that is still owed."""
    owed = retention.make_non_terminal()
    retention.run_retention()
    assert retention.still_exists(owed), (
        f"{retention.name}: a retention pass deleted a non-terminal row. "
        f"Retention must key on terminal state, not on age alone, or it "
        f"destroys the only record that work is still owed"
    )


def assert_retention_prunes_settled_work(retention: Retention) -> None:
    """
    POSITIVE CONTROL: the pass actually removes what the policy calls prunable.

    Without this, the preservation proof passes for a retention pass that never
    deletes anything — including a ``run_retention`` binding that never reaches
    the real pass. Observing the owed row survive is only evidence of a
    predicate once the same pass demonstrably removes a prunable one.
    """
    prunable = retention.make_prunable()
    retention.run_retention()
    assert not retention.still_exists(prunable), (
        f"{retention.name}: the retention pass left a row the domain's own "
        f"policy calls prunable. Either the pass never ran — in which case the "
        f"preservation proof above is vacuous — or make_prunable does not "
        f"build what the policy prunes"
    )


RETENTION_PROOFS: tuple[Callable[[Retention], None], ...] = (
    assert_retention_transition_is_production_bound,
    assert_retention_preserves_non_terminal_work,
    assert_retention_prunes_settled_work,
)


def assert_retention_contract(retention: Retention) -> None:
    for proof in RETENTION_PROOFS:
        proof(retention)
