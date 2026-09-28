# due-work-harness

[![CI](https://github.com/gigaverse-app/due-work-harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/gigaverse-app/due-work-harness/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/due-work-harness)](https://pypi.org/project/due-work-harness/)
[![Python](https://img.shields.io/pypi/pyversions/due-work-harness)](https://pypi.org/project/due-work-harness/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/gigaverse-app/due-work-harness/blob/main/LICENSE)
[![pytest plugin](https://img.shields.io/badge/pytest-plugin-0A9EDC?logo=pytest&logoColor=white)](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![types: Pyrefly](https://img.shields.io/badge/types-Pyrefly-blue)](https://pyrefly.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

**Crash-test your background jobs.** Pytest proofs that background work survives
lost messages, dead workers and uncertain external calls, and that the test
saying so isn't lying.

## Your happy-path test is an optimist

Most applications record something now and finish it later: send the
confirmation email, generate the thumbnails, publish the post, retry the failed
upload. That is *due work*, and it fails quietly:

- **The commit landed. The message didn't.** The order exists; its email never will.
- **The email went out. Then the worker died.** Nothing recorded the send, so recovery sends it again.
- **The stale worker finished last.** It overwrote the fresh result its replacement had just written.

Nothing throws, nothing pages anyone, and the test that runs the happy path once
stays green. `due-work-harness` kills your worker on purpose, after every commit
and after every external call, and fails unless every one of those histories
ends where normal operation does.

## Not theoretical: it found these in code you've heard of

![due-work-harness finding a lost handoff in procrastinate's demo, its fix passing, a repeated notification in DBOS's outbox demo, and the coverage scan of Saleor's checkout](https://raw.githubusercontent.com/gigaverse-app/due-work-harness/main/docs/demo.gif)

<sub>Real output, recorded with [vhs](https://github.com/charmbracelet/vhs) from
[`docs/demo.tape`](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/demo.tape).
CI replays every command it types
([`demos/test_readme_gif.py`](https://github.com/gigaverse-app/due-work-harness/blob/main/demos/test_readme_gif.py)),
so the GIF can't claim output the harness no longer prints.</sub>

The [demos](https://github.com/gigaverse-app/due-work-harness/tree/main/demos) run
the harness against demo applications and a real e-commerce platform, exactly as
their projects ship them, pinned to a commit:

| Where | What one badly timed failure does | What fixes it |
| --- | --- | --- |
| **Saleor** checkout, Payments API | A worker death between capturing the payment and creating the order **charges the customer and never creates the order**. After 90 days the payment belongs to nothing | Saleor's Transactions API with automatic completion of paid checkouts: the same deaths always end with an order |
| **Saleor** checkout | A death after the order commits leaves it unconfirmed, its history empty or half-written | — |
| **Saleor** checkout | No death at all: a failing `order_created` callback, or the broker refusing any one of the order's webhooks, and Django skips the confirmation | — |
| **Saleor** automatic completion | It dispatches a paid checkout again while that checkout's completion is still in flight, and reports no backlog | — |
| **DBOS** `transactional-outbox` | A death after sending the notification, before DBOS records the step: **the customer is notified twice** | An idempotent notification (a key the recipient deduplicates) |
| **procrastinate** `demo_django` | A death between committing the book and deferring its job: **the book is never indexed**, and nothing finds it again | `ATOMIC_REQUESTS = True`: the same proof passes |
| **procrastinate** `demo_django` | A job whose worker died is never picked up again | procrastinate's documented `retry_stalled_jobs` task: the same proof passes |
| **procrastinate** itself | `finish_job` doesn't check the worker: a worker presumed dead finishes a job another worker has since fetched | — (a fencing token on finish) |

Demos are teaching code, and good at what they teach. The point is what the
harness finds when code like this is copied into production, and the one change
that makes the same proof pass. Every finding is a strict xfail with its
explanation, so an upstream fix turns it red. The full write-up:
[`demos/README.md`](https://github.com/gigaverse-app/due-work-harness/blob/main/demos/README.md).

## Install

```bash
pip install due-work-harness            # the core: pytest and pydantic, nothing else
pip install "due-work-harness[django]"  # plus the Django/PostgreSQL integration
```

Or `uv add --dev due-work-harness`. The extras are `[django]`, `[celery]`,
`[procrastinate]` and `[dbos]`; combine as needed. Python 3.12+.

## Kill it on purpose: crash histories

Give the harness one transition that hands work off. It runs normal operation
to learn the outcome, then replays the same transition:

- with **every message it published lost**;
- with the worker **killed right after each commit** it makes;
- with the worker **killed right after each external call** returns;
- with **each after-commit callback failing**, and **the broker refusing each
  publication**, where your host supports them (the Django host breaks
  callbacks; add the Celery integration's breaker to refuse publishes).

Every history must reach the outcome normal operation reached. The harness finds
the commit boundaries itself; you don't name them.

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

A failure names the history that went wrong and what it left behind:

```text
AssertionError: orders: handoff 'place order': normal operation reaches ('SENT', 1), but these
histories reach something else: {'worker died after external call 1': ('SENT', 2)}. Work was lost
or repeated. ...
```

That's the duplicate email, caught in a test instead of in a support ticket.
When a test can't reach the worker at all (a workflow engine's own executor),
`process_histories` runs the real program in a child process, kills it at named
points with `os._exit`, restarts it the way production would, and applies the
same verdict. That is how the DBOS demo above works.

## Six ways due work goes missing: lifecycle profiles

A crash history proves one handoff. A declarative `DueWorkContract` binds your
production selection, tick and transitions, and asks for a disposition (claim,
decline with a reason, not applicable, or known gap) for each of six profiles:

| Profile | The question it answers |
| --- | --- |
| **A** automatic recovery | After a lost message, is the work still found? Does anything actually run the sweep, index-served and bounded? |
| **B** bounded ownership | Who owns the work right now, and what happens when that owner's lease expires? |
| **C** crash ambiguity | The provider never answered. Did the email go out? Schrödinger's email: can you tell "never ran" from "maybe ran"? |
| **D** durable retention | Can a cleanup pass delete work that is still owed? |
| **E** eventual convergence | When a result lands late, can it overwrite a newer one? |
| **F** fact-derived obligations | Can product state imply work nothing recorded, and is it still found? |

Every contract also disposes of two execution-safety profiles: replay safety and
bounded retry. Read [what a green result means](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/what-a-green-result-means.md)
before you treat a pass as a guarantee.

## Green you can trust

A conformance suite can lie in ways ordinary tests can't. An adapter that copies
production's query, or implements a tiny state machine in the test file, makes
every proof measure the copy, and it stays green forever. So the harness:

- refuses test-authored selections and transitions, and requires every binding
  to reach code in your `production_packages`;
- forbids waiving its own integrity checks;
- pairs every "must not happen" proof with a positive control showing the same
  binding *would* act;
- fails a run where every crash history agrees but recovery changed nothing:
  agreeing with an inert recovery is not a pass.

The full catalogue of lies it refuses: [false greens](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/false-greens.md).

## No handoff left behind: the coverage scan

A crash history proves one handoff; the coverage check makes sure none are
forgotten. It scans production code for every call that hands work off:
`on_commit`, a Celery `.delay`, a procrastinate `.defer`, a DBOS workflow start,
a Dramatiq `.send`, an RQ `enqueue`, a Django task's `.enqueue`. It attributes
each to the exact function that makes it, and requires exactly one disposition
per function: a contract suite that insures it, or an exemption that proves
losing it costs nothing. In Saleor it finds 137 handoff sites in 124 functions.

```bash
uv run due-work-harness check      # static: imports neither your app nor your tests
```

```python
@due_work_contract_suite(ORDER_NOTIFICATIONS, covers=(DueWorkSource(OrderService.place),))
class TestOrderNotificationsDueWork:
    pass
```

Aliases, re-exports and handoffs passed along uncalled are still found. A
declaration counts only if pytest would actually run it, and
`pytest --due-work-verify` then checks that it did. An existing project adopts
with a baseline that only shrinks.

In GitHub Actions, `gigaverse-app/due-work-harness/check@v0` runs the scan and
`gigaverse-app/due-work-harness/test@v0` runs the generated suites (every
generated case carries the `due_work` mark). The path from install to CI is in
[ADOPTING.md](https://github.com/gigaverse-app/due-work-harness/blob/main/ADOPTING.md).

## Bring your own framework

The core depends only on pytest and pydantic: no Django, SQLAlchemy, Celery,
procrastinate or DBOS, and CI imports every core module with none of them
installed. Proofs take plain callables. The few facts only a framework knows
(whether a transaction is open, how to interrupt a commit, what plan a query
runs) come from a `Host` you configure once:

```python
# conftest.py
from due_work_harness import configure
from due_work_harness.integrations.django import django_host

configure(django_host(production_packages={"myapp"}))
```

| Extra | Supplies |
| --- | --- |
| `[django]` | `django_host()`: pytest-django marks, the transaction probe, a commit counter that kills the worker after any commit (including writes made through `SELECT fn()`), PostgreSQL plan inspection, lifecycle-state proofs |
| `[celery]` | beat-schedule evidence, a publication recorder that holds messages instead of sending them, a publication breaker that refuses one publish as a broker that is down would |
| `[procrastinate]` | its worker as recovery, "worker died holding this job" arrangement, the documented stalled-job recipe, periodic-task evidence |
| `[dbos]` | restarting an app through its own startup for process-level crash histories |

## Status

Alpha. The proofs were extracted from a production codebase, where they guard
its background workflows in CI; the public API may still change before 1.0.
Python 3.12+. How it's built: [ARCHITECTURE.md](https://github.com/gigaverse-app/due-work-harness/blob/main/ARCHITECTURE.md).

## Development

Managed with [uv](https://docs.astral.sh/uv/) (`uv sync --all-extras`), linted with Ruff and
type-checked with [Pyrefly](https://pyrefly.org/). See [CONTRIBUTING.md](https://github.com/gigaverse-app/due-work-harness/blob/main/CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](https://github.com/gigaverse-app/due-work-harness/blob/main/LICENSE).
