# due-work-harness

**Proofs that your background work survives lost messages, dead workers and
uncertain external calls — and that the test saying so isn't lying.**

Most applications record something now and finish it later: send the
confirmation email, generate the thumbnails, publish the post, retry the failed
upload. That is *due work*. Its failures are quiet: the commit lands and the
queue message doesn't; a worker dies between sending an email and recording
that it did; a stale worker finishes after its replacement and overwrites the
newer result. Everything looks fine in a test that runs the happy path once.

`due-work-harness` states the properties due work must have as executable
pytest proofs, and runs them against **your production code** — whatever queue,
job library or workflow engine it uses.

```text
pip install due-work-harness            # the core: pytest and pydantic only
pip install "due-work-harness[django]"  # plus the Django/PostgreSQL integration
```

## What it checks

**Crash histories.** Give it one transition that hands work off. It runs normal
operation to define the outcome, then replays the same transition with every
message lost, with the worker killed right after *each* commit it makes, and
killed right after *each* external call — and requires every history to reach
the same outcome. It finds commit boundaries itself; you don't name them.

```python
from due_work_harness import CallableDelivery, ExternalCall, HandoffHistory, assert_crash_at_every_commit_converges


def test_placing_an_order_survives_any_death():
    assert_crash_at_every_commit_converges(
        CallableDelivery(name="orders", recover=run_workers_until_idle),  # what production runs after a crash
        HandoffHistory(
            name="place order",
            arrange=new_cart,
            transition=place_order,  # your real view or service method
            observe=lambda cart: (order_status(cart), mailbox.count(cart)),  # include what external systems saw
            external_calls=(ExternalCall(mailer, "send"),),
        ),
    )
```

A failure names the history and what it reached:

```text
AssertionError: orders: handoff 'place order': normal operation reaches ('SENT', 1), but these
histories reach something else: {'worker died after external call 1': ('SENT', 2)}. Work was lost
or repeated. ...
```

**Lifecycle profiles.** A declarative `DueWorkContract` binds your production
selection, tick and transitions, and requires a disposition — claim, decline
with a reason, not applicable, or known gap — for each of six profiles:

| Profile | The question it answers |
| --- | --- |
| **A** automatic recovery | After a lost message, is the work still found? Does anything actually run the sweep, index-served and bounded? |
| **B** bounded ownership | Who owns the work right now, and what happens when that owner's lease expires? |
| **C** crash ambiguity | When the provider's answer never arrives, can you tell "never ran" from "maybe ran"? |
| **D** durable retention | Can a cleanup pass delete work that is still owed? |
| **E** eventual convergence | When a result lands late, can it overwrite a newer one? |
| **F** fact-derived obligations | Can product state imply work nothing recorded — and is it still found? |

Plus two execution-safety contracts every adopter disposes: replay safety and
bounded retry. See [what a green result means](docs/what-a-green-result-means.md) and [the ways a green suite can lie](docs/false-greens.md).

**Tests that can't be faked green.** A conformance suite fails differently from
ordinary tests: an adapter that copies production's query, or implements a tiny
state machine in the test file, makes every proof measure the copy. The
harness refuses test-authored selections and transitions, requires every
binding to reach code in your `production_packages`, forbids waiving its own
integrity checks, and pairs every "must not happen" proof with a positive
control that shows the same binding *would* act. When every crash history
agrees, recovery must still have changed something — agreement with an inert
recovery is not a pass.

## Every handoff accounted for

A crash history proves one handoff; the coverage check makes sure none are
forgotten. It scans production code for every call that hands work off —
`on_commit`, a Celery `.delay`, a procrastinate `.defer`, a DBOS workflow start —
attributes each to the exact function that makes it, and requires exactly one
disposition per function: a contract suite that insures it, or an exemption
that proves losing it costs nothing. Aliases, re-exports and handoffs passed
along uncalled are still found, and a declaration counts only if pytest would
actually run it; `pytest --due-work-verify` then checks that it did.

```python
@due_work_contract_suite(ORDER_NOTIFICATIONS, covers=(DueWorkSource(OrderService.place),))
class TestOrderNotificationsDueWork:
    pass
```

```bash
uv run due-work-harness check      # static: imports neither your app nor your tests
```

In GitHub Actions: `gigaverse-app/due-work-harness/check@v0` for the scan, and
`gigaverse-app/due-work-harness/test@v0` for the generated suites (every
generated case carries the `due_work` mark). The path from `uv add` to CI is in
[ADOPTING.md](ADOPTING.md).

## Framework-free by construction

The core depends only on pytest and pydantic — no Django, SQLAlchemy, Celery,
Procrastinate or DBOS. Proofs take plain callables. The few facts only a
framework knows — whether a transaction is open, how to interrupt a commit,
what plan a query runs — come from a `Host` you configure once:

```python
# conftest.py
from due_work_harness import configure
from due_work_harness.integrations.django import django_host

configure(django_host(production_packages={"myapp"}))
```

| Extra | Supplies |
| --- | --- |
| `[django]` | `django_host()`: pytest-django marks, the transaction probe, a commit counter that kills the worker after any commit (including writes made through `SELECT fn()`), PostgreSQL plan inspection, lifecycle-state proofs |
| `[celery]` | beat-schedule evidence, a publication recorder that holds messages instead of sending them |
| `[procrastinate]` | its worker as recovery, "worker died holding this job" arrangement, the documented stalled-job recipe, periodic-task evidence |
| `[dbos]` | restarting an app through its own startup for process-level crash histories |

Where a test can't reach the worker at all — a workflow engine's own executor —
`process_histories` runs the real program in a child process, kills it at named
points with `os._exit`, restarts it the way production would, and applies the
same verdict. CI imports every core module with none of the frameworks
installed.

## Demos: the harness against code we didn't write

[`demos/`](demos/) runs the harness against demo applications exactly as their
projects ship them, pinned to a commit:

| Demo | Finding | Fix the positive control proves |
| --- | --- | --- |
| procrastinate `demo_django` | A worker death between committing the book and deferring its job leaves the book never indexed | `ATOMIC_REQUESTS = True` |
| | `index_book` holds a transaction across its slow call | — |
| | A job whose worker died is never picked up again | procrastinate's documented `retry_stalled_jobs` task |
| DBOS `transactional-outbox` | A death after sending the notification, before DBOS records the step, notifies the customer twice | an idempotent notification |

The coverage scan finds the same handoffs statically: the create view's `.defer` and `index_book`'s in the
procrastinate demo, and `DBOS.start_workflow` in DBOS's other outbox variant. A handoff made in SQL, as
`transactional_enqueue.py` does, is invisible to a static scan, which is where a crash history takes over.

Demos are teaching code; these are the things to change when copying them into
production, and the harness shows each fix working.

## Status

Alpha. The proofs were extracted from a production codebase, where they guard
its background workflows in CI; the public API may still change before 1.0.
Python 3.12+. Architecture: [ARCHITECTURE.md](ARCHITECTURE.md).

## Development

Managed with [uv](https://docs.astral.sh/uv/) (`uv sync --all-extras`), linted with Ruff and
type-checked with [Pyrefly](https://pyrefly.org/). See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
