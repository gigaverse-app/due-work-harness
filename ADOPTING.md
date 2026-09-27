# Adopting due-work-harness

This is the path from "we use Django/Celery, Procrastinate or DBOS" to "CI fails
when background work can be lost or repeated". A complete, minimal adopter lives
in [`examples/adopter/`](examples/adopter/); the harness's own CI runs it through
the GitHub Actions below.

## 1. Install

```bash
uv add --dev "due-work-harness[django]"     # or [celery], [procrastinate], [dbos]; combine as needed
```

The core needs only pytest. An extra adds that framework's integration.

## 2. Configure the scan

```toml
# pyproject.toml
[tool.due-work-harness]
production-packages = ["myapp"]   # the code that must be accounted for
```

Every installed integration's handoff sites are scanned by default:

| Kind | A site is |
| --- | --- |
| `django` | `transaction.on_commit(...)`, including through `sync_to_async` |
| `celery` | `.delay(...)` / `.apply_async(...)` on a `@shared_task` / `@app.task`, and `send_task(...)` |
| `procrastinate` | `.defer(...)` / `.defer_async(...)` on an `@app.task`, including after `.configure(...)` |
| `dbos` | `DBOS.start_workflow(...)`, and `queue.enqueue(workflow, ...)` for a `@DBOS.workflow` |

Narrow it with `sites = ["django", "celery"]`. See
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

The proof runs as a test. An exemption without one is refused.

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
selects exactly the adoption suites.
