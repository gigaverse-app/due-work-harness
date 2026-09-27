"""
The six lifecycle profiles: one module per question an adopter must answer.

Each profile is a capability a durable-work lifecycle may or may not have. A
``DueWorkContract`` gives every profile a disposition: ``Claim`` it and the
module's proofs run against the adopter's real bindings, or ``Decline`` /
``NotApplicable`` it with a reason. Which to claim is decided by the decision
tree in ``docs/durable-work-conformance-harness.md#the-six-lifecycle-profiles``.

* A ``AUTOMATIC_RECOVERY`` — ``automatic_recovery/``: is outstanding work still
  found after a lost message, and does something run the real sweep?
* B ``BOUNDED_OWNERSHIP`` — ``bounded_ownership``: who owns the work now, and
  what happens when that authority expires?
* C ``CRASH_AMBIGUITY`` — ``crash_ambiguity``: can we tell "never ran" from
  "maybe ran" when the answer never arrives?
* D ``DURABLE_RETENTION`` — ``durable_retention``: does a scheduled pass delete
  rows, and can it take one that is still owed?
* E ``EVENTUAL_CONVERGENCE`` — ``eventual_convergence``: when a result lands
  late, can it overwrite a newer one?
* F ``FACT_DERIVED_OBLIGATIONS`` — ``fact_derived_obligations``: can product
  facts imply work nothing recorded, and is it still found?

Adopters import the binding types from here (``DueWorkSweep``,
``FencedOwnership``, …) but never call the proofs directly: the contract
generates them. Adding a profile is a design change; see
``docs/durable-work-conformance-harness.md#developing-a-new-profile-or-standalone-proof``.
"""
