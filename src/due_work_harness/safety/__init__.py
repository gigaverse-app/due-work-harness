"""
Execution-safety profiles: what happens when the same work executes twice, or keeps failing.

These are separate from the six lifecycle profiles. Every adopter gives both a
disposition in its ``SafetyContract``, because every worker can be redelivered
and every external call can fail.

* ``replay_safe_execution`` — ``ReplaySafeEffect``: executing one logical
  effect twice reaches the external boundary twice and still converges to a
  single visible result.
* ``bounded_retry`` — ``BoundedRetry``: a retryable failure has a finite,
  stable lifecycle. Production selects it, each execution makes one real
  failing attempt, it exhausts exactly on budget, and the terminal state stays put.
"""
