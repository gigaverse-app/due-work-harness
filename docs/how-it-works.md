# How the proofs work

Most applications record something now and finish it later: send a receipt,
generate a thumbnail, publish an order event, or retry an upload. That is *due
work*. A happy-path test runs it once and misses the boundary where the record
commits but the message does not, or the external effect happens but the worker
dies before recording it.

## Crash histories

Give the harness one production transition and a bounded recovery path. It
learns the normal outcome, then replays the transition with messages lost,
worker death after each commit or external call, and supported callback,
receiver, publication, or lost-reply faults. Every history must reach the
normal outcome. The host detects commit boundaries; the adopter does not name
them.

```python
from pytest_obligation import CallableDelivery, ExternalCall, HandoffHistory, assert_crash_at_every_commit_converges


def test_placing_an_order_survives_any_death():
    assert_crash_at_every_commit_converges(
        CallableDelivery(name="orders", recover=run_workers_until_idle),
        HandoffHistory(
            name="place order",
            arrange=new_cart,
            transition=place_order,
            observe=lambda cart: (order_status(cart), mailbox.count(cart)),
            external_calls=(ExternalCall(mailer, "send"),),
        ),
    )
```

That standalone probe is useful for exploration, but a complete domain
adoption must put its histories in an `ObligationContract` and expose a collected
class with `@due_work_contract_suite(CONTRACT)`. The decorator generates pytest
cases at collection time; it does not write Python test files. Histories can
declare their expected `Findings`, including a strict gap for an observed
divergence. See the [required adoption shape](../ADOPTING.md#required-adoption-shape).

When the production worker cannot be interrupted in-process, `process_histories`
runs it in a child process, kills it at named points with `os._exit`, and
restarts it through the real application entry point. The DBOS demo uses this
path. A history's observation must include externally visible effects, not
just the database row that requested them.

## The A–J guarantees

A declarative contract binds production selection, tick, transitions, and
recovery, then gives each profile a truthful disposition: claim, decline with
evidence, not applicable, known gap, or not assessed. A–J are the current
source catalog; use the guide matching your installed release.

| Profile | Question |
| --- | --- |
| **A** automatic recovery | After a lost message, is owed work found by a scheduled, bounded sweep? |
| **B** bounded ownership | Who owns the work, and what happens when its lease expires? |
| **C** crash ambiguity | If the provider never answered, can recovery tell whether the effect happened? |
| **D** durable retention | Can cleanup delete work that is still owed? |
| **E** eventual convergence | Can a late result overwrite a newer one? |
| **F** fact-derived obligations | Can product state imply work that nothing recorded, and is it found? |
| **G** gated execution | Does blocked work stay owed and untouched, then recover once eligible? |
| **H** harmless replay | Does repeating one logical operation converge to one visible effect? |
| **I** indivisible admission | Does a command commit product intent and follow-up work together? |
| **J** job retry limits | Does failing work exhaust its budget and remain terminal? |

Profile E has independently assessed stale-snapshot, monotonic-result,
in-flight, and evidence-confluence families. The harness generates
deterministic competing-event histories; optional Hypothesis explores more
schedules. See [interleavings](interleavings.md) and the [executed adopter
capability map](adopter-capabilities.md).

`NotAssessed` creates a strict XFAIL with a remediation reason; it does not
mean a production bug was demonstrated. A passing sibling profile or family
cannot clear that debt. `--due-work-profile-report=profiles.json` distinguishes
declarations from executed evidence, including selection, teardown failures,
and xdist. `--due-work-require-assessed` can make unfinished assessment a CI
failure. An xfail is never a verified guarantee.

## Guarding against false greens

A test-only implementation of production's query or state machine can make a
suite green while measuring a copy. The harness rejects test-authored
selections and transitions, requires bindings to reach configured production
packages, refuses waivers for integrity checks, pairs negative proofs with
positive controls, and rejects inert recovery where nothing was actually
completed. These safeguards do not make a pass a universal correctness proof:
it means the specific generated cases ran against the stated bindings.
Read [false greens](false-greens.md) and [what a green result means](what-a-green-result-means.md).

Use `pytest --due-work-summary` to list generated cases and their outcomes,
`--junitxml=<artifact-path>` for a standard machine-readable pytest artifact,
and `--due-work-profile-report=profiles.json` for executed profile evidence.
