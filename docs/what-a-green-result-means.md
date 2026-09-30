# What a green result means

`due-work-harness` turns a contract about background work into a pytest suite.
This page explains why the contract is split into profiles, what a passing or
failing case actually tells you, and what the proofs deliberately do not cover.
Read it before you treat a green suite as a guarantee.

## Why profiles, not one interface

The part of durable background work worth sharing across codebases is the
contract, not a runtime: whatever library or hand-written loop runs the work,
the invariants it must keep are the same. A contract that cannot be executed is
a wish, so this package states each invariant as an assertion an adopter runs
against its own tables and code.

How the contract is decomposed matters as much as the assertions. No adopter
needs every capability. A stateless re-dispatch sweep needs discovery and
nothing else; a non-idempotent call to an external provider needs all of it. A
single "durable effect" interface would force most adopters to stub out most of
it. Profiles let an adopter declare what it claims and be measured only on that.

| Profile | Module | Answers |
| --- | --- | --- |
| A, automatic recovery | `profiles.automatic_recovery` | What work is outstanding? Is it still found after a lost message? Does anything actually run the sweep, and is the adapter describing the real one? |
| B, bounded ownership | `profiles.bounded_ownership` | Who owns the work right now, and what happens when that owner dies? |
| C, crash ambiguity | `profiles.crash_ambiguity` | What if the provider never answered? |
| D, durable retention | `profiles.durable_retention` | Can cleanup delete work that is still owed? |
| E, eventual convergence | `profiles.eventual_convergence` | When a result lands, can it overwrite a newer one? |
| F, fact-derived obligations | `profiles.fact_derived_obligations` | Can an obligation exist that nothing ever recorded? |

| G, gated execution | `profiles.gated_execution` | Is blocked work preserved, prevented from running early, and recovered when eligible? |
| H, harmless replay | `profiles.harmless_replay` | Does a real repeated execution leave one visible logical effect? |
| I, indivisible admission | `profiles.indivisible_admission` | Does the standalone command roll back product intent and obligations after partial admission? |
| J, job retry limits | `profiles.job_retry_limits` | Does production enforce its retry budget and stable terminal state? |

All ten decisions live in `DueWorkContract.profiles`. `SafetyContract` is a scoped
H/J-only declaration for effects outside a complete due-work domain adoption.

E has four independent families: stale snapshots, monotonic results, in-flight
completion, and evidence confluence. Claiming one does not certify the others.
`NotAssessed` is strict-XFAIL assessment debt; `KnownGap` records a demonstrated
or declared missing guarantee in legacy behavior. Neither is passing evidence.
The [migration guide](../ADOPTING.md#migrating-an-existing-declaration) explains
how to declare the distinction and produce an executed coverage report.

The deterministic catalogs and optional Hypothesis explorer use the same
invariant runner. [Interleavings](interleavings.md) explains the guarantees and
limits: they exercise controlled application boundaries, not arbitrary CPU or
SQL instruction schedules. Exploration is additional search, never exhaustive proof.

### Execution eligibility: owed is not the same as runnable

Work can be owed and blocked at once: a dependency has not settled, an owner is
still active, a user has not confirmed. That is profile G, declared as `eligibility=` (one `ExecutionGate`, or named gates for
named blockers) on a contract that claims profiles A and G with a sweep, because
recovery is what must find the work once it is eligible. It is not a seventh
disposition: a domain without a product-level blocker has no gate to describe.

Each gate is one blocked, already time-due obligation with its readiness
notification lost. Five proofs run against a fresh example each:

- the bindings reach production: the routes, recovery, both selections
  (`due_work` and `owed_work`) and the readiness transition `make_eligible`;
- blocked work stays owed and unselected, and no route or recovery executes it,
  inspects it early, calls the provider or writes anything (including a write
  that restores the same state, which an independent mutation count sees);
- once eligible, recovery alone completes it, and readiness released the
  existing obligation without a new revision, a reset retry budget or an
  earlier not-before time;
- the fallback inspection (`recheck_after`) happens on its declared boundary
  from admission, probed one `clock_resolution` before the boundary and across
  it by moving the clock; then, from that inspection, not before the boundary
  again, and within the recovery timeout after it (a continuation delay may push
  it later, never earlier, and it must happen). `clock_resolution` is how early an
  inspection may fire unseen, so it is at most one second and a tenth of
  `recheck_after`. The inspection must not call the provider, change the product
  state or drop the obligation, and must not repeat without time passing. It
  counts as an execution unless the gate declares `inspections`, for a design
  that re-checks the blocker without executing anything;
- the gate describes the contract sweep's own recovery: the sweep's selection
  leaves the blocked work out and takes it in once eligible, and during
  `recover` the sweep's `dispatched_ids` (the recorder of what its dispatch
  path sent, which the contract sweep must declare) records the gate's identity
  once more; a readiness notification sent through the same path earlier does
  not count against it. What is observed is the dispatch, not which code ran where, so a tick
  reached through a service, a task queue or another thread counts alike. The
  gate's `identity` is what the sweep's `identity_of` reports for the same row.

A pass says a scheduler with blocked work neither loses it nor runs it early. It
does not say the blocker is the right product rule.

### F asks the question underneath A

Profile A asks whether recorded work survives a lost message. Profile F asks
whether the work was ever recorded at all. The difference is edge-triggering
versus level-triggering, and the failure F catches is invisible to every other
profile: an obligation nothing wrote down produces no row, so discovery,
ownership and ambiguity all pass by having nothing to look at.

### Declining is an answer, not a gap

Every profile needs a disposition: `Claim`, `Decline`, `NotApplicable` or
`KnownGap`. The contract refuses to construct without one for each profile,
because the profile you left out is exactly the one that would have revealed
the defect.

A decline is a substantive statement. Consider a sender that is edge-triggered
by design: a batch of messages exists because a planning step wrote it, and
"already sent" is not visible in the product state that asked for the send, so
there is nothing to re-derive the obligation from. That adopter declines
profile F, and it can back the decline with evidence: pointing F at it fails
the discovery proof (`assert_unrecorded_obligation_is_discovered`) and no
other. `DisprovenCapability` runs that check against production bindings. This
is the same discrimination check the package asks for everywhere else, applied
to a profile instead of to an implementation.

Missing work is not a decline. If the domain *should* have a capability and
does not, that is a `KnownGap`, a strict xfail that stays visible until it is
fixed.

## The adapter is part of what is tested

Every behavioural proof is only as true as the bindings the adopter supplies.
If an adapter describes a selection that production does not run, every proof
about that selection measures the description. That is why each profile opens
with guards about the adapter itself, before any behaviour is tested:

- **Authorship.** The binding tripwires reject selections and transitions
  written in test code, including copies hidden behind test helpers.
  "Production" means code in the host's `production_packages`.
- **Agreement.** Profile A also compares the adapter's two production paths:
  it runs the real tick and checks that the rows it dispatched are the rows the
  adapter's own selection describes. The expected value comes from evaluating
  another production binding, not from something written in the test.

The two checks are not redundant. The authorship check cannot tell whether two
production paths agree. The agreement check cannot tell whether either of them
is production at all, because a transcription that happens to be accurate today
satisfies it. You need both.

These guards cannot be waived. A gap declaration naming one of them is a design
error, because an xfail there would strict-xfail the defence itself and leave
every other proof measuring whatever the adapter says.

## A pass and a failure are not symmetric

Reading them as if they were is the most likely way to misuse this package.

**A failure is evidence.** "An index scan on the primary key with every
predicate in the filter" is a full table scan, run once a minute by the sweep.
"Nothing selects the in-flight state" means a dead worker strands its row
forever. "No schedule entry runs the recovery tick" means a lost message is
never recovered. These are checkable facts, not opinions, and they do not
become less true because someone disagrees about whether they matter.

**A pass is not evidence of correctness.** It means the specific defects these
proofs look for are absent. Nothing more. A person chose the invariants, and
choosing where to look is a judgment even when each observation is not. The
[known gaps in coverage](#known-gaps-in-coverage) below are the shape of that
judgment, made visible.

### A real finding discriminates

A failure proves something about the pair (code, proof), not automatically
about the code. The provider-transaction proof
(`assert_provider_call_holds_no_transaction`) once failed against innocent
code, because the test setup wrapped every test in a transaction, so "a
transaction is open" was true everywhere. The proof now refuses to run when the
test itself holds a transaction.

Telling the two apart is cheap and objective, not a matter of taste. That
failure appeared identically for every adopter, including ones known to be
correct, which is the signature of an instrumentation fault. If you suspect a
proof, point it at an implementation you believe is correct. If it still fails,
the proof is what is wrong.

### Silence is the more dangerous failure

Of the first defects found in these proofs themselves, most were false
negatives: the index proof staying silent about a real full scan, more than
once. Only one was a false positive. Silence is the more common failure and the
more dangerous one, because a missing finding looks exactly like approval.

This is why the package refuses silence wherever it can: every profile needs a
disposition, every gap is a *strict* xfail that trips when the gap is fixed, and
a `KnownGap` is strongest with a `detect` probe that fails exactly while the gap
exists.

### What a crash history absorbs

A transition interrupted on purpose raises things, and a history has to decide
which of them are the interruption and which are defects. One rule, in this
order, for every host:

1. A seam's refusal (a deferred result the harness cannot observe honestly) is
   raised, even when production swallowed it or wrapped it in its own error.
2. After a simulated worker death, everything is absorbed, a failed assertion
   included: it was raised on the way out (a connection close, a `finally`, a
   cleanup's check), which a dead process never runs. A death raised inside a
   task group arrives as a group holding `WorkerDied`, and is the death.
3. Otherwise a failed assertion, bare or anywhere inside an exception group,
   is raised: an invariant failing is a defect whatever happened before it.
4. After an injected failure (a failed after-commit callback or signal
   receiver, a refused publication, a lost reply), an exception is absorbed
   when it is that failure, is raised from it (`raise ... from error`), is a
   deliberate translation of it (`raise ... from None` in its handler), or is
   a group made only of such exceptions: that is the application's own
   response, as a real request errors.
5. Anything else fails the history. An error raised inside the failure's
   handler without `from` is linked to it only implicitly and is usually a bug
   in the handler; it, and an error not linked at all, carry a note saying to
   chain it if it is a deliberate response.

The Gigaverse backend's copy of the harness applies the same rule.

A divergence always raises `HistoriesDiverged`, and a declared handoff gap is a
strict xfail for that alone; a broken binding, a failed positive control or an
observation that differs between two clean runs is a plain `AssertionError`.

## Where the proofs came from

Several proofs came from comparing hand-written background work with what a
durable-execution runtime does by construction:

- **The tick is actually scheduled** (`assert_the_tick_is_actually_scheduled`).
  A runtime enrols work by construction. A hand-written sweep can be perfect and
  unscheduled, and every other invariant would pass it.
- **Claims are exclusive across real connections**
  (`assert_claim_is_exclusive_across_connections`). Two claims race on two
  database connections rather than being simulated in one.
- **No transaction is held across a provider call**
  (`assert_provider_call_holds_no_transaction`). Runtimes commonly enforce this
  internally, and it applies just as much to hand-written code.
- **One failing row does not stall the tick**
  (`assert_one_failing_row_does_not_stall_the_tick`). This came from asking what
  a runtime's worker loop does that a hand-written loop does not. It found a
  sweep where one failing dispatch aborted the whole tick. Because the selection
  was deterministic and oldest-first, the same row would have blocked the same
  backlog on every tick, forever.

## Known gaps in coverage

These are named on purpose, because a contract's silence looks the same as its
approval.

1. **Owner death is partly simulated.** Profile B simulates a dead owner by
   letting its lease expire. Crash histories (`crash_histories`) kill the
   worker right after each commit, using the host's worker killer, and
   `process_histories` kills a real child process at death points the adopter
   names. None of these interleaves
   two workers at arbitrary statement boundaries. A domain whose correctness
   depends on a specific interleaving still needs a targeted race test of its
   own.
2. **Races run on a barrier, not at chosen points.** The two-connection
   exclusivity proof releases both claims together and checks that exactly one
   wins. That is narrower than a test that pauses one connection at a specific
   statement, but it is a real race on real connections. An earlier version of
   this list said a portable contract could not express two-connection races
   at all. That was wrong. The difference between "a contract cannot express
   this" and "nobody has written it yet" is exactly the kind of thing a
   contract should not be trusted to judge about itself.
3. **Provider reconciliation is out of scope.** Profile C proves that late
   evidence *can* resolve an ambiguous attempt and that terminal state is
   monotonic. How a domain obtains that evidence (a provider's message IDs, a
   listing of recent posts, a deterministic external identifier) is
   domain-specific and cannot be contracted.
4. **Deterministic-identity convergence has no profile.** Some domains converge
   by deriving the external object's identity deterministically (for example
   one identifier per revision) rather than by rejecting stale writes. Profile E
   does not fit them without distortion. A deterministic-identity profile would
   be a real addition once more than one adopter needs it.
5. **Logic without a callable seam cannot be tested.** When recovery logic
   lives inside one large query inside a task, with no function an adapter can
   call, no contract can bind it without refactoring first. Treat that as a
   finding in its own right: an invariant nobody can test is an invariant
   nobody can rely on.
6. **Multi-step memoisation is not modelled.** Work whose completed steps must
   not re-run after a crash, the shape durable workflow engines provide, is a
   different primitive and has no profile here.
7. **A profile is only as validated as its independent adopters.** The
   strongest evidence that a profile states the right invariant is an
   implementation written before the contract existed that passes it
   unmodified. A profile with one claimant and one declared decline shows that
   it discriminates, but not that it generalises. A profile with no adopters
   is defined and self-tested, and nothing more.
