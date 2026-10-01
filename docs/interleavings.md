# Generated interleavings and replay

Interleavings drives real application commands while external requests or evidence
arrivals are held at declared boundaries. It is the execution engine for two
independent **Profile E** families: `in_flight` and `evidence_confluence`. The
profiles describe guarantees; the engine supplies schedules that challenge them.

## What is generated

An `InFlightConvergence` declaration generates applicable histories for clean
execution, refusal before application, applied effects with lost responses,
accepted requests completing late, competing revisions in both completion orders,
a return to an earlier value, retirement, independent progress, and notification
loss/redelivery. Capabilities and explicit limitations select the catalog. Each
history ends with bounded recovery and checks both convergence and quiet-state
stability. A repeated visible payload does not excuse corrupt revision or
acknowledgement identities. Seams listed in `acknowledgement_only_seams` also
get acknowledgement-without-application histories (below); nothing else does.

### Acknowledgement without application (opt-in per seam)

Test the failures the dependency's declared contract permits. An API that
guarantees completion when it replies, such as a MongoDB write acknowledged
under its configured write concern or an API that answers once the operation is
done, may be trusted without a read-back; keep testing its crashes and lost
replies, and do not opt it in. An API that only acknowledges receipt is
different. Shopify's App Events API answers HTTP 202 `{"success": true}` for
every billing event, including events it rejects later, and the only read-back is
an aggregate meter in a different API. Its reply says the event arrived, not that
billing happened. Only such a seam belongs in `acknowledgement_only_seams`:

```python
billing = InFlightConvergence(
    ...,
    seams=("send_billing_event", "store_subscription_snapshot"),
    acknowledgement_only_seams=("send_billing_event",),
)
```

Each named seam must be one of `seams`. For each, the catalog adds
`IF.ack-without-apply/<seam>`, which arms `Fault.ACKNOWLEDGE_WITHOUT_APPLYING`:
the provider returns an ordinary reply (the seam's `reply()` when it passes one,
otherwise `None`), never runs `perform`, and counts the call in
`ProviderControl.unapplied_acknowledgements` instead of `effects`. The invariant,
`acknowledged-not-applied`, is that production does not confirm that revision
from the reply:

- It is checked right after the reply, before any recovery, so a writer that
  confirms from the receipt while the provider observation still differs from
  the reviewed expectation fails, even if later recovery repairs it. A replay-safe
  writer that retries and independently verifies application before its start
  operation returns may already be confirmed at this checkpoint. Replay-unsafe
  calls still cannot be repeated, even if the repeated call applies successfully.
- With `replay_safe=True` (the default) the history must then settle to the
  reviewed expectation. `observe` reads provider state, so that needs recovery
  that checks the provider or sends again.
- With `replay_safe=False` sending again is refused (`non-repeatable`), so the
  history checks after recovery as well that the revision is still unconfirmed.
  An unresolved or ambiguous disposition (see Profile C), or a rejection learned
  from provider evidence, conforms.
- Retirement exposes no acknowledgement identity, so a retirement declaration
  only checks convergence after recovery when replay-safe, and reports a
  limitation when it is not. Only opted-in declarations get that limitation.

A declaration without `acknowledgement_only_seams` generates exactly the catalog,
limitations and diagnostics it generated before this option existed, and not
opting in produces no warning, gap or required explanation. Exploration does not
arm this fault; if it ever does, the same opt-in will bound it.

The fault is one-shot, like the others: once consumed, the next invocation at the
seam may apply normally. It does not model a provider that permanently rejects an
idempotency key (Shopify consumes the key of a rejected event, so replaying it
cannot repair the rejection). That rule, the evidence sources (a read-back, a
meter, a listing) and the recovery a provider calls for belong to the adopter's
simulator and tests. The core injects the reply and checks that it did not
confirm the effect; it has no opinion on burned keys or billing recovery.

An `EvidenceConfluence` declaration generates permutations, duplicates, partial
fact arrivals with recovery between them, and batching partitions when supported.
Dependencies constrain legal orders. Optional ordered actors and retry/replay
histories exercise independently declared seams. Observations and exact effect
counts are checked against reviewed expectations for every reachable fact subset;
the runner never learns its expected outcome from the run under test.

One binding therefore produces many pytest cases automatically. The number depends
on capabilities, aliases, seams and dependencies; inspect `scenario.histories()`
or `pytest --collect-only` for the actual count. Histories with identical steps
are deduplicated while retaining the families they cover. Missing prerequisites,
unreached seams and invalid bindings fail instead of creating vacuous passes.

These histories explore semantic boundaries, not every CPU instruction or SQL
statement interleaving. A pass covers the declared observations, fault seams,
recovery budget and generated schedules. Missing domain observations or boundaries
can still hide failures.

## Binding an application

Public interfaces live in `due_work_harness.interleavings`: `InFlightConvergence`,
`InFlightSession`, `Intent`, `EvidenceConfluence`, `EvidenceSession`,
`EvidenceArrival`, `EvidenceExpectation`, `EvidenceRetry`, `Bounds`,
`ProviderControl`, `PendingRequest`, `Transport`, `KnownFailure`, `HistoryTrace`
and `replay_history`. Fields and callbacks are documented on those canonical
models in [`bindings.py`](../src/due_work_harness/interleavings/bindings.py),
[`model.py`](../src/due_work_harness/interleavings/model.py) and
[`ports.py`](../src/due_work_harness/interleavings/ports.py).

```python
from due_work_harness import Claim, ConvergenceFamily, NotApplicable, Profile
from due_work_harness.interleavings import InFlightConvergence

# bind_avatar yields a fresh InFlightSession for each history or search example.
avatar = InFlightConvergence(
    name="avatar-revisions",
    bind=bind_avatar,
    intents=("first", "second", "third"),
    seams=("write-avatar",),
    independent=True,
    transport=True,
)

# On the application's otherwise complete DueWorkContract:
# profiles={..., Profile.E: Claim()},
# in_flight={avatar.name: avatar},
# convergence_families={
#     ConvergenceFamily.EVIDENCE_CONFLUENCE:
#         NotApplicable("This workflow has no independent evidence arrivals."),
# },
```

A binding context arranges fresh resources, routes production calls through an
external fake, and yields independently observed state. Add the normal `ARRANGE`,
`REAL PRODUCTION`, `EXTERNAL SEAM` and `OBSERVE` comments beside those callbacks.
The core never imports Django or a queue. The configured host supplies database
marks; generated histories request real commits where a host requires them.

`ProviderControl()` uses the portable `AcceptedProviderRequest` by default; inject
`accept=` to retain requests through an existing external fake. It counts attempted
calls, effects `perform` actually applied, and unapplied acknowledgements
separately. Its
`accept` callback retains an external-only completion function as a
`PendingRequest`. Completion must change the external fake only, never acknowledge
or repair application rows. A provider accepting a request is distinct from
applying it and from the caller receiving its response. Production admission,
revision changes, receipt consumption and recovery remain production code.

## Optional search

Fixed histories and saved replay need only the core installation. Install
Hypothesis explicitly for additional generated schedules:

```bash
uv add --dev 'due-work-harness[exploration]'
uv run pytest -m due_work --due-work-explore=smoke
uv run pytest -m due_work --due-work-explore=deep
```

Normal runs default to `off` and deselect exploration methods. Smoke uses up to
20 examples with a 15-step budget; deep uses up to 200 with a 50-step budget.
Hypothesis may shrink failures using additional executions. Every execution and
shrink enters a fresh binding context. Search respects causal prerequisites,
uses deterministic settings and needs no persistent Hypothesis database.
The integration rejects explicit exploration without its dependency with an
installation hint. Neither importing the core nor fixed replay imports Hypothesis.

## Reproduce and track failures

An invariant failure includes a JSON `HistoryTrace` containing aliases and steps,
not serialized application state. Save it as a regression artifact:

```python
from pathlib import Path
from due_work_harness.interleavings import HistoryTrace, replay_history

trace = HistoryTrace.model_validate_json(Path("avatar-regression.json").read_text())
replay_history(avatar, trace)
```

Replay checks scenario identity and versions. Increase a scenario's `version`
when its aliases or binding semantics change incompatibly.

Legacy deterministic gaps name an exact generated history and invariant through
`KnownFailure`. Only that pair is strict-XFAIL; a different invariant, binding
error or newly passing history fails the test. New-feature contracts reject
those gaps. Exploration does not suppress failures using fixed-history waivers.
Unbound E families are separate `NotAssessed` XFAILs: they identify unfinished
assessment, not a reproduced production defect.

`--due-work-profile-report=profiles.json` reports declared family coverage and
execution of the expected deterministic cases. Exploration reports its own pytest
outcome; it does not turn a finite search into a whole-profile guarantee.
