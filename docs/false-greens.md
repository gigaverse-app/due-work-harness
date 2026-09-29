# False greens: the ways a conformance suite can lie

A conformance suite fails differently from ordinary tests. A contract bound to
something other than production proves things about that something, with the
full authority of the profile report. Every row below was found by pointing the
harness at real adopters and asking how the green result could be wrong; each is
now refused mechanically, and each refusal is self-tested in both directions.

If you are writing an adapter and catch yourself doing one of these to get a
proof green, the proof was about to tell you something.

## Bindings that are not production

| The lie | What it looks like | Refused by |
| --- | --- | --- |
| **The copied predicate** | `due_work=lambda: Job.objects.filter(status="todo")` restating production's query | the authorship tripwire: a semantic binding in test code may not call ORM query or write methods, including through test helpers |
| **The in-memory counterfeit** | a dict-based state machine in the test module, which authors no ORM call | the delegation tripwire: a semantic binding must reach code in the host's `production_packages` |
| **The self-referential differential** | `dispatched_ids=lambda: [r.pk for r in due_work()[:page]]`, so "what the tick dispatched" is compared with itself | a recorder must be empty before the tick runs |
| **The test-authored tick** | `run_tick` re-implemented in the test | delegation on `run_tick` |
| **The waived defense** | declaring a gap on the tripwire itself, then authoring freely | binding-integrity proofs cannot be named as gaps |
| **The synthetic decline** | `pytest.raises` around a shared proof fed a fabricated adapter, "proving" production lacks a capability | test-authored inversions are refused; `DisprovenCapability` guards the binding before disproving anything |
| **The minted proof name** | a helper named `assert_whatever` in the test module standing in for a shared proof | delegation resolves to real harness callables, not names |
| **The unreachable lifecycle** | a state transition written as a direct column update, reaching states production never can | transition bindings get both tripwires |
| **The adopter-authored verdict** | `is_due=lambda row: not is_terminal(row)` agreeing with the test's own state machine | membership is computed by the harness from the production selection |
| **The self-computed backlog** | `observe_outstanding=lambda: Job.objects.filter(...).count()` compared against the backlog it was computed from | the reading must reach production and must not aggregate in the adapter |

## Proofs that pass because nothing happened

| The lie | What it looks like | Refused by |
| --- | --- | --- |
| **The vacuous negative** | a "must not write" proof whose binding never reaches the write path | every negative proof has a positive control: the same binding must act on owed work |
| **The single-branch example** | a selection with several ways to be true, exercised by examples that all take one branch | one named `OwedWorkVariant` per branch, each proven selected and dispatched |
| **Lifecycle theater** | `run_once` advances counters without reaching the dependency whose failure drives retries | each execution must reach the injected failing boundary exactly once |
| **Fresh means in flight** | a row too young for the recovery query is called "in flight" though nothing started it | the in-flight example must be in the production selection before it starts |
| **The replay that never ran** | a replay-safety check whose second run finds the row settled and no-ops | the replay must reach the external boundary a second time (count goes 0 → 1 → 2) |
| **The index that reads the history** | a selection served by an index that is walked end to end, or a bitmap over a whole index, and rechecked away: the scan-ratio proof saw no `Filter` to count | rows removed by an index recheck count, per loop; `assert_selection_cost_does_not_grow_with_the_history` (opt-in) compares buffers and rows visited against a full-table read, with the history required to be large enough to separate anything, and the owed rows required to be found |
| **Agreement with an inert recovery** | a crash history whose recovery does nothing, so every history agrees with an equally unfinished normal operation | when every history converges, recovery must have changed the observation in at least one |

## Histories that miss the failure

| The lie | What it looks like | Refused by |
| --- | --- | --- |
| **Two coherent halves of different wholes** | profile F's outstanding predicate and profile A's selection each green, disagreeing about which work exists | a composition proof whenever both are claimed |
| **Association without meaning** | naming a publisher whose stranded work this sweep's selection never sees | each covered publisher's stranded work is run through the real selection and tick |
| **Terminal attempt, live obligation** | a state labelled terminal while a successor is still owed from it | every lifecycle state must be declared; delivering each terminal example to the real worker must create no new obligation |
| **The message-only handoff** | a transition commits a failure and only publishes the work that creates its retry | a crash history's lost-notification run must reach normal operation's outcome |
| **The write that reads as a SELECT** | a failure written by `SELECT some_function(...)` in autocommit, then a handoff in a second statement: one commit counted, the split never crashed | autocommit `SELECT`/`WITH`/`VALUES` statements, and `INSERT`/`UPDATE`/`DELETE`/`MERGE` (a trigger or predicate function can write while the statement reports zero rows), run in a one-statement transaction during a history; an assigned transaction id counts the write |
| **The observer that owns the transaction** | the commit counter issues its own `BEGIN`/`COMMIT` around a statement inside the caller's transaction, so a rollback the code under test relies on no longer rolls back, or a raw-SQL `COMMIT` is never counted | the server's transaction status is consulted, not only Django's flag: inside a caller-owned transaction nothing is begun or ended, and a SQL `COMMIT` counts at its own boundary |
| **The repeat no commit boundary shows** | notify, then record completion; every crash after a commit converges, a death after the notification sends it twice | `HandoffHistory.external_calls`: a death right after each named external call must converge too |
| **The death before the effect** | an async client, or a sync SDK method returning deferred work: the call "returns" a coroutine at once, the worker dies there, the effect never happens, and recovery's one notification looks exactly right | an awaitable result is awaited before the call is counted or the death injected; a coroutine created before the death is closed, never run in cleanup |
| **The seam that vouches for another** | two declared seams, one called: "some external call happened" is true, so a stale or misspelled declaration passes and its deaths are never tried | every declared seam must be called by the transition, checked per seam |
| **The race with one runner missing** | two claims raced on two connections; one blocks on the other's uncommitted claim and never returns, and counting only the racer that came back finds exactly one winner | a racer still running when the race's time is up fails the proof; the Django host bounds each racer's statements and closes its connection on every exit |
| **The handoff behind another callback** | the order commits, then two plain `on_commit` callbacks run; a death after the commit is recovered by a sweep, but a *failing* first callback makes Django skip the second, and nothing selects what it owed | `callback_breaker`: each after-commit callback failing in turn must converge too; `robust=True` only protects the callbacks after the failing one |
| **The hook that rewrites the record** | a worker runs the task, records it SUCCESSFUL, then sends `task_finished`; every death converges, but a receiver that raises lands in the worker's own failure path, which records the task FAILED after its effect happened | `receiver_breaker`: each receiver of the worker's signals failing in turn must converge too |
| **The handoff behind a publish** | a callback publishes a webhook or a task, then the handoff runs; with the broker up every history converges, but a refused publish raises out of the callback and takes everything after it down | `publication_breaker`: the broker refusing each publication in turn must converge too |

## Blocked work

| The lie | What it looks like | Refused by |
| --- | --- | --- |
| **The blocker that drops the work** | a blocked obligation deleted or rewritten by a sweep that cannot run it yet, so readiness later releases nothing | every route and recovery is run while blocked; the obligation must stay owed and its reserved state (revision, retry budget, not-before) unchanged |
| **The hidden write** | a blocked attempt restores the same state after touching it, so any before/after comparison of state passes | an independent durable-mutation count must not move while blocked |
| **The early run** | a blocked obligation executes, or calls the provider, on a sweep before it is eligible | executions and provider calls must not move while blocked; the example must not already be complete |
| **The release that admits new work** | becoming eligible bumps the revision, resets the retry budget or moves the not-before time forward | the reserved state must be identical before and after readiness |
| **The lost notification nobody re-checks** | readiness is signalled once; the signal is lost and no sweep ever looks again, or completion is faked without reaching the provider | recovery alone must reach the declared outcome, and the provider must be reached |
| **The fallback on the wrong side of its boundary** | the periodic inspection fires a second early, never fires, fires on every run because inspecting does not move its next inspection, or re-arms after one second instead of `recheck_after` | `recheck_after` is probed one `clock_resolution` before and at the boundary by moving a clock, in two windows (from admission, then from the first inspection); a second run without time passing must not inspect again |
| **The fallback that runs the blocked work** | the periodic inspection calls the provider, completes the work or drops it while it is still blocked; "it inspected" is all that was checked | at each inspection, provider calls, product state and the owed obligation must be unchanged; with `inspections` declared, executions too |
| **The counterfeit around the verdict** | a test-written `owed_work` that always reports the obligation, or a `make_eligible` that flips a flag instead of running production's transition | both are held to the production-binding guard, with `due_work`, the routes and `recover` |
| **The gate beside the sweep** | a gate whose `recover` invokes the worker directly and whose `due_work` is some other query: every eligibility proof passes while the sweep never releases the work | the contract sweep's selection must agree with the gate on the blocked and eligible work, and `recover` must enter the code the sweep's tick runs |

## Observations that cannot fail

- **The observation recovery erases.** A project's own cleanup (a periodic task that deletes sent mail, expired
  rows, finished jobs) removes what the contract observes, so a history that lost the work and one that did it
  look identical after recovery. Refused by review only: observe what the cleanup leaves, and before trusting a
  green history ask what a run that lost the work would have shown.

## What only a reviewer can refuse

- **A production reference that is not load-bearing.** The delegation tripwire
  proves a binding reaches production, not that production does the work. A
  binding that calls production and quietly post-processes the result is caught
  in review.
- **Example constructors.** `make_*` factories are arrange code and may write
  directly, so they can build states production never produces. Review them
  against the production writers.
- **Patching inside a semantic binding.** Replacing an external provider at its
  seam is legitimate; patching your own lifecycle code inside a binding neuters
  the thing under test. Name the seam in a comment.
- **Observation width.** An `observe` that omits what the handoff creates, or
  what the external system saw, weakens every "nothing changed" and every crash
  history to nothing.
