"""
The harness's own conforming references, for its self-tests only.

Every proof needs a positive control: a lifecycle that passes, and focused
mutants of it that must fail. These modules are those lifecycles, owned by the
harness so that a proof is never validated against the adopter it judges.

* ``in_memory`` — in-memory reference machines for profiles B–F and the
  safety profiles. Framework-free, like the rest of the core.
* ``in_memory_handoffs`` — a ledger with interruptible commits, for crash histories.
* ``eligibility`` — an independent scheduler with sixteen injectable faults, for the
  execution-eligibility proofs.

References that need a real database live with their integration, for example
:mod:`due_work_harness.integrations.django.references`.

NEVER import these in an adopter's contract: binding a reference measures the
reference, not production.
"""
