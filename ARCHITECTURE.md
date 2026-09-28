# Architecture

`due-work-harness` states invariants about **due work** — work a program records
now and a worker completes later — as executable proofs. The proofs are
framework-free. Frameworks reach them only through one small interface, the
**host**, and through optional **integrations**.

## Rules the code follows

1. **The core imports no framework.** Nothing under `due_work_harness/` outside
   `integrations/` imports Django, SQLAlchemy, Celery, Procrastinate, DBOS, or
   any database driver, at module level or inside functions. The runtime
   dependencies are `pytest` and `pydantic`. CI proves it by importing every core module in an
   environment with none of them installed.
2. **An integration imports its framework, and nothing imports an integration
   implicitly.** `due_work_harness.integrations.django` may import Django; the
   core never imports it. Users opt in with an extra
   (`pip install due-work-harness[django]`) and by configuring a host.
3. **Proofs take production callables, never framework objects.** A selection is
   `Callable[[], Iterable[Any]]`; a tick is `Callable[[], int]`; a transition is
   `Callable[[Handle], object]`. Where a proof needs a fact only the database
   knows (a query plan, whether a transaction is open, how to interrupt a
   commit), it asks the host, and fails with a message naming the missing
   capability if the host cannot answer.
4. **Adapters bind production code; they do not reproduce it.** The binding
   tripwires in `binding.py` reject test-authored selections and transitions.
   What counts as production is the host's `production_packages`.

## Models

Structured values are Pydantic models built on `due_work_harness.models`, never
dataclasses:

- `HarnessModel` (frozen, `extra="forbid"`, arbitrary types allowed) for
  declarations and results; `MutableHarnessModel` for state a proof accumulates.
- Construction is by keyword. A class whose public API is naturally positional
  (`DueWorkSource(fn)`, `ExternalCall(owner, "attr")`, `Decline("why", prove=...)`)
  defines `__init__(self, first=MISSING, /, **data)` and forwards
  `with_positional(data, field=first)`: Pydantic validates through `__init__(**fields)`,
  so the positional parameter must also accept its field by name.
- Checks that ran in `__post_init__` run in `model_post_init` and raise
  `DueWorkContractDesignError` (or another non-`ValueError`) so the error reaches
  the caller as itself; Pydantic wraps `ValueError`/`AssertionError` from there.
- Copies use `model.model_copy(update={...})`. On a `HarnessModel` that copy is
  validated like a new value (`model_post_init` runs, unknown keys are refused),
  so a changed declaration cannot skip its design checks. Private state uses
  `PrivateAttr`.
- A user's own dataclasses and Pydantic models remain welcome as *observed
  values*; the rule is about the harness's own types.

## The host

`due_work_harness.host.Host` (configure once per session with
`due_work_harness.configure(...)` or the `due_work_harness_host` ini option):

| Capability | Type | Used by |
| --- | --- | --- |
| `production_packages` | `frozenset[str]` | binding tripwires |
| `database_marks(transactional)` | `Callable[[bool], Sequence[pytest.Mark]]` | generated contract cases |
| `in_transaction()` | `Callable[[], bool] \| None` | `assert_provider_call_holds_no_transaction` |
| `worker_killer(kill_after)` | `WorkerKiller \| None` | crash histories (death after each commit) |
| `callback_breaker(fail_at)` | `CallbackBreaker \| None` | crash histories (each after-commit callback failing) |
| `publication_breaker(refuse_at)` | `PublicationBreaker \| None` | crash histories (the broker refusing each publication; `celery_publication_breaker` for Celery) |
| `receiver_breaker(fail_at)` | `ReceiverBreaker \| None` | crash histories (each receiver of the signals an adopter names failing; `django_receiver_breaker(*signals)` for Django) |
| `connection_scope()` | `Callable[[], ContextManager]` | two-connection races (profile B) |
| `selection_inspectors` | `tuple[SelectionInspector, ...]` | profile A database proofs: `index_served(selection)`, `replica_read(selection)`, `scan_counts(selection)`, `statements_during(run)` |
| `ambient_context()` | `Callable[[], object] \| None` | profile A `AMBIENT_CONTEXT_PROOFS`: a tick restores ambient context (opt-in; fails when unset) |
| `publication_recorder()` | `Callable[[], ContextManager[list[str]]] \| None` | lifecycle-state proof 2c: work handed off only as a queue message |
| `frozen_clock(moment)` | `Callable[[datetime], ContextManager] \| None` | lifecycle-state proofs: aging rows past the recovery delay |
| `sweep_proofs` | `tuple[Callable, ...]` | profile A proofs only a framework can run (the Django host adds 2b/2c) |

`WorkerKiller.__call__(kill_after: int | None)` returns a context manager
yielding an object with `commits: int`, `dead: bool` and `kill_now(reason: str)`.
Inside it, every commit on the calling thread's connection is counted; the worker
dies (raises `due_work_harness.worker_death.WorkerDied`, drops after-commit
callbacks) right after commit `kill_after`; once dead, every later statement
raises `WorkerDied`, except rollbacks; on exit a dead worker's session is closed.

## Package layout

```
due_work_harness/
  __init__.py            public API (re-exports)
  host.py                Host, configure, current_host, hosted
  models.py              HarnessModel, MutableHarnessModel, with_positional
  pytest_plugin.py       the due_work_harness_host ini option; --due-work-verify
  worker_death.py        WorkerDied
  binding.py             adapter tripwires (authorship, delegation)
  helpers.py             contract_params, undeclared, assert_provider_call_holds_no_transaction
  coherence.py           cross-profile coherence
  contract.py            DueWorkContract, SafetyContract, ScheduledSelection, suites
  gap_probes.py          executable KnownGap probes
  crash_histories.py     HandoffHistory, ExternalCall, Delivery, histories and verdict
  process_histories.py   deaths of a real child process (stdlib only)
  exemptions.py          exempt_due_work_suite: a proven "losing this is fine"
  coverage/              the static check: every handoff site has one disposition (imports nothing it scans)
    sites.py             what a site is: kinds per framework
    project.py           module discovery and static name resolution (imports, re-exports)
    handoffs.py          handoff sites in production, by outermost function
    declarations.py      contract and exemption declarations in test modules
    report.py            Site, Disposition, CoverageReport
    scan.py              the rules that match the two
    config.py            [tool.due-work-harness]
    cli.py               due-work-harness check | sites | baseline
  evidence/              observation reports
  safety/                ReplaySafeEffect, BoundedRetry
  profiles/
    automatic_recovery.py   profile A (DueWorkSweep and its proofs)
    bounded_ownership.py    profile B
    crash_ambiguity.py      profile C
    durable_retention.py    profile D
    eventual_convergence.py profile E
    fact_derived_obligations.py profile F
  references/in_memory.py   conforming in-memory implementations for self-tests
  integrations/
    django/    django_host(), worker killer, write classification,
               QuerySet SelectionInspector, lifecycle-state proofs (2b/2c)
    celery.py  beat schedule evidence
    procrastinate.py  worker recovery, stalled-job arrangement, periodic evidence
    dbos.py    relaunch helper
demos/       the harness run against unmodified upstream demo applications
examples/adopter/  a minimal adopting project, run by CI through the actions
check/action.yml   GitHub Action: the static coverage check
test/action.yml    GitHub Action: the generated due_work suites
tests/core   self-tests with no framework installed
tests/django self-tests against PostgreSQL through the Django integration
```

## Cross-module interfaces

- `crash_histories.Delivery`: `name: str`; `session() -> ContextManager[DeliverySession]`.
  `DeliverySession`: `deliver()` runs what the transition published;
  `lose()` drops it (or `can_lose = False` when there is nothing separate from
  the database to lose, as with a PostgreSQL job queue); `recover()` runs the
  bounded normal recovery production performs.
  `CallableDelivery(name, recover, deliver=None, lose=None)` covers most hosts.
- `crash_histories.assert_crash_at_every_commit_converges(delivery, history)`.
- `crash_histories.assert_histories_converge(name, runs: list[HistoryRun])`,
  host-neutral, for deaths a host arranges itself (for example a real process).
- `profiles.automatic_recovery.DueWorkSweep.due_work: Callable[[], Iterable[Any]]`.
  Plan proofs call `current_host().inspector_for(selection, capability=...)`.
- Schedule evidence is a callable the adopter supplies
  (`assert_scheduled=celery_beat_evidence("pkg.task")`, or
  `procrastinate_periodic_evidence("pkg.task")`); the core never reads a
  framework's settings.
