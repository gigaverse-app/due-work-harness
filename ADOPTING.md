# Adopting due-work-harness

This is the path from "we use Django/Celery, Procrastinate or DBOS" to "CI fails
when background work can be lost or repeated". A complete, minimal adopter lives
in [`examples/adopter/`](examples/adopter/); the harness's own CI runs it through
the GitHub Actions below.

## Required adoption shape

Every due-work domain adoption **MUST** declare an `ObligationContract` and expose a
collected class decorated with `@due_work_contract_suite(CONTRACT)`. Declare all
ten profiles A–J with truthful dispositions;
bind claimed profiles to production code. Put crash histories in `handoffs=` or
`process_handoffs=` so the generated suite owns their execution and reporting.

A file of ordinary tests, a `HandoffHistory`, direct calls to
`assert_crash_at_every_commit_converges`, or a helper named `contract()` returning
something else **does not satisfy this requirement**. Those are useful supporting
regressions or exploratory probes, not a completed domain adoption. Harness
self-tests and independently scoped safety/selection/exemption suites retain
their own APIs; they must not be presented as a full due-work domain contract.

The decorator generates **pytest cases at collection time**, not Python source
files. Verify the generated node IDs with `pytest --collect-only`, then run with
`--due-work-summary`. For a machine-readable generated file, pass pytest's
`--junitxml=<artifact-path>` and retain it as a CI artifact. Link the declarations,
suite classes, and run report in the PR. Report passes, declared gaps, declines,
and exclusions separately; collection alone is not execution evidence. Do not
invent a claim or misuse `Decline` to hide unfinished bindings. Legacy gaps must
remain explicit; new-feature contracts cannot waive gaps.

## Migrating an existing declaration

This version requires Pydantic 2.11 or newer; the minimum is exercised in CI.
The canonical names and enum values now follow the A–J vocabulary in the
[profile table](docs/how-it-works.md#the-a-j-guarantees). This is an API migration:

- Move nested `safety.profiles` into `ObligationContract.profiles`: old
  `SafetyProfile.REPLAY_SAFE_EXECUTION` becomes `Profile.HARMLESS_REPLAY` (H),
  and `SafetyProfile.BOUNDED_RETRY` becomes `Profile.JOB_RETRY_LIMITS` (J).
  Move `replay=` and `retry=` onto the same `ObligationContract`, preserving their
  fixtures, real-commit requirement and legacy gap policy.
- Add G and I decisions. Claim G when binding `eligibility=`; claim I when binding
  named `admission=` commands. Use `NotAssessed(because="...")` when assessment
  is unfinished. It produces a strict XFAIL until replaced with evidence or an
  applicability decision. Existing H/J test IDs remain stable.
- `SafetyContract` remains a standalone H/J-only view for independently scoped
  effects, using `Profile.H` and `Profile.J`. It is no longer nested in a domain
  contract. The implementations live in `profiles.harmless_replay` and
  `profiles.job_retry_limits`; G lives in `profiles.gated_execution`.
- E's four families are independently assessed. Existing bindings imply a claim
  for their own family only. Unbound families generate assessment XFAILs; declare
  `convergence_families={ConvergenceFamily.IN_FLIGHT: NotApplicable("reason")}`
  only when that family truly cannot apply. Exact behavioral failures belong in
  `Claim.gaps` or the scenario's history/invariant gaps, not blanket family waivers.

`pytest --due-work-profile-report=profiles.json` records every expected behavioral
case, which cases were collected and selected, and their setup/call/teardown
outcomes. It supports xdist and never treats an XFAIL as a verified guarantee.
This complements the static handoff scan and `--due-work-verify`; neither a
collected declaration nor one passing test certifies a whole profile.

See [interleavings](docs/interleavings.md) for generated competing-event tests,
optional exploration and deterministic replay. Profile I's callbacks are documented
in [`indivisible_admission.py`](src/pytest_obligation/profiles/indivisible_admission.py):
bind a real standalone command, independently observed product intent and work,
an external-publication recorder, and a fault boundary reached after partial writes.
The host must supply `in_transaction`; fault fixtures must not supply the transaction.

## 1. Install

```bash
uv add --dev "pytest-obligation[django]"     # or [celery], [procrastinate], [dbos]; combine as needed
```

The core needs only pytest and pydantic. An extra adds that framework's integration.

## 2. Configure the scan

```toml
# pyproject.toml
[tool.due-work-harness]
production-packages = ["myapp"]   # the code that must be accounted for
```

A kind is scanned whenever production code imports its framework, directly or
through another module of the project, so the result is the same wherever the
check runs, framework installed or not:

| Kind | A site is |
| --- | --- |
| `django` | `transaction.on_commit(...)`, including through `sync_to_async` |
| `celery` | `.delay(...)` / `.apply_async(...)` and their `_on_commit` variants on a `@shared_task` / `@app.task`, and `send_task(...)` |
| `procrastinate` | `.defer(...)` / `.defer_async(...)` on an `@app.task`, including after `.configure(...)` |
| `dbos` | `DBOS.start_workflow(...)`, and `queue.enqueue(workflow, ...)` for a `@DBOS.workflow` |
| `dramatiq` | `.send(...)` / `.send_with_options(...)` on an `@actor` |
| `rq` | `enqueue`, `enqueue_call`, `enqueue_at`, `enqueue_in` on `rq`/`django_rq` or a queue they returned; `.delay(...)` on an `@job` |
| `django-tasks` | `.enqueue(...)` / `.aenqueue(...)` on a `django.tasks` (or `django_tasks`) `@task` |

A site is any reference to the handoff, called or not, through any alias: a
local name, a parameter default, `self.hook`, a re-export, `sync_to_async(...)`
or `functools.partial(...)` all count, in the outermost function that contains
them. `sites = ["celery"]` adds a kind the scan cannot detect (a framework
reached only through a third-party wrapper); it never removes a detected one.
A project helper that hands work off for its callers (Zulip's
`send_event_on_commit`, say) is declared in `bridges` with its own site count, so
each call to it becomes a site in its caller. `exclude` adds name patterns to
skip, and may never hide production code. See
[`coverage/config.py`](src/pytest_obligation/coverage/config.py) for every key.

## 3. See what you have, and baseline the past

```bash
uv run due-work-harness sites      # every site, and its disposition
uv run due-work-harness baseline   # a [tool.due-work-harness.baseline] table for today's sites
```

Paste the baseline into `pyproject.toml` to adopt without fixing everything at
once. The baseline records work that predates adoption and **only shrinks**: in
CI, `check --base-ref <base>` refuses any entry a change adds.

## 4. Configure the host for your tests

```python
# conftest.py
from pytest_obligation import configure
from pytest_obligation.integrations.django import django_host

configure(django_host(production_packages={"myapp"}))
```

The host tells the framework-free proofs what only your framework knows: how
to reach the database, count commits and freeze time.

If your code publishes through Celery, let crash histories refuse each publish
as a broker that is down would:

```python
from pytest_obligation.integrations.celery import celery_publication_breaker

configure(django_host(production_packages={"myapp"}, publication_breaker=celery_publication_breaker))
```

Name the signals whose receivers matter to your work, and crash histories fail
each receiver in turn, as one with a bug or an unreachable backend would. If
your migrations seed rows your code needs (a CMS's root page), keep them across
committing cases:

```python
from django.tasks.signals import task_finished, task_started

from pytest_obligation.integrations.django.receivers import django_receiver_breaker

configure(
    django_host(
        production_packages={"myapp"},
        receiver_breaker=django_receiver_breaker(task_started, task_finished),
        serialized_rollback=True,
    )
)
```

On django-tasks-db, recovery is its worker:
`integrations.django_tasks.db_worker_once()` runs `manage.py db_worker --batch`.
`integrations.django_tasks.worker_contract(name=..., enqueue=..., effect=...)`
is django-tasks-db's own contract with that worker, bound to one of your tasks:
you enqueue it and say how to see its effect, and the framework's dispositions,
retention proof and known gaps come with it.

To pin what every crash history of a handoff leaves — a findings table that
names exactly which history moved when something changes — use
`assert_pinned_outcomes(delivery, history, delivered=..., outcomes=...)`, and give
the test the host's database marks with `@due_work_database()`.

## 5. Give each site a disposition

**Cover it with a contract**: the proofs that it survives lost messages and
dead workers. Name the exact function the contract insures:

```python
@due_work_contract_suite(ORDER_NOTIFICATIONS, covers=(DueWorkSource(OrderService.place),))
class TestOrderNotificationsDueWork:
    pass
```

Start small by declaring a `HandoffHistory` in the contract's `handoffs=` with
its real `handoff_delivery=`; the suite generates the crash proof. Direct helper
calls may help discovery, but must be incorporated into the declaration before
calling adoption complete. See [the README](README.md) and
[what a green result means](docs/what-a-green-result-means.md).

If the work can be owed but *blocked* by a product decision (a dependency has
not settled, an owner is still active), add an `ExecutionGate` to a contract
that claims profile G: `ObligationContract(..., eligibility=order_blocked_by_payment)`,
or a mapping of names to gates for several blockers. Its binding routes,
recovery and selection call production; the harness proves the blocked work
stays owed and untouched, and completes by recovery alone once eligible, with
the readiness notification lost. Native worker recovery can bind G without claiming
A. When A is also claimed, the contract's sweep must declare `dispatched_ids`,
the recorder of what its dispatch path sends: the gate's recovery is shown to be
the sweep's by the gate's identity being dispatched there while it runs. See
[what a green result means](docs/what-a-green-result-means.md#execution-eligibility-owed-is-not-the-same-as-runnable).

**Or exempt it, with proof**, when losing the handoff genuinely costs nothing:

```python
@exempt_due_work_suite(
    DueWorkSource(summaries.rename_order),
    reason="the read path rebuilds a summary that disagrees with its order",
    prove=LossIsAbsorbedElsewhere(strand=..., observe=..., absorb=summaries.summary),
)
class TestSummaryEvictionExemption:
    pass
```

The proof runs as a test. An exemption without one is refused, and so is a
proof written in the test itself (a lambda, a local function): it must be a
harness probe such as `LossIsAbsorbedElsewhere`, whose `absorb` is the
production path the reason names.

Each call then removes its function from the baseline.

## 6. Enforce it in CI

```yaml
jobs:
  due-work-check:
    # Static: no database, no framework, seconds. Pins the harness version from your uv.lock.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: gigaverse-app/pytest-obligation/check@v0

  due-work-suites:
    # The generated contract, safety and exemption suites, in your own environment.
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_PASSWORD: postgres }
        ports: ["5432:5432"]
    steps:
      - uses: actions/checkout@v7
      - uses: gigaverse-app/pytest-obligation/test@v0
        with:
          sync-args: --all-extras
          pytest-args: --ds=myproject.settings
```

Without GitHub Actions, the same two steps are:

```bash
uv run due-work-harness check --base-ref origin/main
uv run pytest -m due_work
```

Every case the harness generates carries the `due_work` mark, so `-m due_work`
selects exactly the adoption suites. The `test` action also passes
`--due-work-verify`, which fails the session unless every declaration the check
counted ran a case: a suite skipped by an `importorskip`, deselected or never
collected cannot keep its function accounted for. Add it to the plain command
too:

```bash
uv run pytest -m due_work --due-work-verify
```

A declaration the check counts is one pytest will run as written: a
module-level `Test*` class, not rebound later in its module, with no skip or
xfail mark, using the harness's own decorator, `DueWorkSource` and contract.


### Executed adoption and optional search

The [catalog example](examples/adopter/README.md) supplies working G/H/I bindings,
revision and retirement histories, delayed provider effects, lost/duplicate wakeups,
and batched/duplicate receipts including retry turnover. The same bindings run
through RQ, Celery and Prefect in this repository's integration suites.

`--due-work-require-assessed` rejects collected contracts containing `NotAssessed`
profiles or E families **before selection filters**. It is optional: ordinary
adopters still get actionable XFAIL debt while migrating. It does not certify
unimported suites or make a Decline/known gap green; combine it with
`--due-work-verify` and `--due-work-profile-report=profiles.json` for enrollment
and executed evidence. The shipped demos enable it in CI.
