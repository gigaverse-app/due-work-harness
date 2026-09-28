# Adopting due-work-harness

This is the path from "we use Django/Celery, Procrastinate or DBOS" to "CI fails
when background work can be lost or repeated". A complete, minimal adopter lives
in [`examples/adopter/`](examples/adopter/); the harness's own CI runs it through
the GitHub Actions below.

## 1. Install

```bash
uv add --dev "due-work-harness[django]"     # or [celery], [procrastinate], [dbos]; combine as needed
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
[`coverage/config.py`](src/due_work_harness/coverage/config.py) for every key.

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
from due_work_harness import configure
from due_work_harness.integrations.django import django_host

configure(django_host(production_packages={"myapp"}))
```

The host tells the framework-free proofs what only your framework knows: how
to reach the database, count commits and freeze time.

If your code publishes through Celery, let crash histories refuse each publish
as a broker that is down would:

```python
from due_work_harness.integrations.celery import celery_publication_breaker

configure(django_host(production_packages={"myapp"}, publication_breaker=celery_publication_breaker))
```

## 5. Give each site a disposition

**Cover it with a contract**: the proofs that it survives lost messages and
dead workers. Name the exact function the contract insures:

```python
@due_work_contract_suite(ORDER_NOTIFICATIONS, covers=(DueWorkSource(OrderService.place),))
class TestOrderNotificationsDueWork:
    pass
```

Start small: a `HandoffHistory` with `assert_crash_at_every_commit_converges` is
often the first proof worth having. See [the README](README.md) and
[what a green result means](docs/what-a-green-result-means.md).

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
      - uses: gigaverse-app/due-work-harness/check@v0

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
      - uses: gigaverse-app/due-work-harness/test@v0
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
