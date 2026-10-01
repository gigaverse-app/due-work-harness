# due-work-harness

<img src="plugins/due-work-harness/assets/icon.svg" alt="Due Work Harness logo" width="96">

[![CI](https://github.com/gigaverse-app/due-work-harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/gigaverse-app/due-work-harness/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/due-work-harness)](https://pypi.org/project/due-work-harness/)
[![Python](https://img.shields.io/pypi/pyversions/due-work-harness)](https://pypi.org/project/due-work-harness/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/gigaverse-app/due-work-harness/blob/main/LICENSE)
[![pytest plugin](https://img.shields.io/badge/pytest-plugin-0A9EDC?logo=pytest&logoColor=white)](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![types: Pyrefly](https://img.shields.io/badge/types-Pyrefly-blue)](https://pyrefly.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

**Generate strong, standardized tests for work you cannot afford to lose or
repeat.** Declare a `DueWorkContract` against your application's real code and
the harness generates pytest cases for crash recovery, worker ownership,
uncertain external calls, retention, convergence, and replay safety. Your coding
agent can also use the same fault-injection infrastructure to write focused
tests for your particular workflow. Did a Celery task disappear after a Django
transaction committed? Can a retry charge twice, resend a receipt, or process
a Kafka message again? These are testable questions, not assumptions that your
queue or framework already makes the work safe.

The core takes plain Python callables and assumes no particular framework,
database, queue, or business domain. Optional adapters expose specific failure
points in Django/PostgreSQL, django-tasks, Celery, RQ/Redis, MongoDB, Prefect,
and aiokafka. A Shopify integration or another external API can be tested
through the application's real Python call and observable effects; that does
not imply a built-in Shopify adapter or coverage of every provider behavior.

**What it is, in 45 seconds** (with sound):

https://github.com/user-attachments/assets/498ac071-080c-45d2-abdb-13aee405568b

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
| **django-tasks-db** (Django's task framework, database backend) | A `task_finished` receiver that raises rewrites a task that already ran as FAILED; a task whose worker died stays RUNNING forever | — |
| **Wagtail** on django-tasks-db | Deleting an image or document can leave its file in storage for good; publishing can leave the CDN serving the old page | — |
| **Saleor** checkout, Payments API | A worker death between capturing the payment and creating the order **charges the customer and never creates the order**. After 90 days the payment belongs to nothing | Saleor's Transactions API with automatic completion of paid checkouts: the same deaths always end with an order |
| **Saleor** checkout | A death after the order commits leaves it unconfirmed, its history empty or half-written | — |
| **Saleor** checkout | No death at all: a failing `order_created` callback, or the broker refusing any one of the order's webhooks, and Django skips the confirmation | — |
| **Saleor** automatic completion | It dispatches a paid checkout again while that checkout's completion is still in flight, and reports no backlog | — |
| **DBOS** `transactional-outbox` | A death after sending the notification, before DBOS records the step: **the customer is notified twice** | An idempotent notification (a key the recipient deduplicates) |
| **procrastinate** `demo_django` | A death between committing the book and deferring its job: **the book is never indexed**, and nothing finds it again | `ATOMIC_REQUESTS = True`: the same proof passes |
| **procrastinate** `demo_django` | A job whose worker died is never picked up again | procrastinate's documented `retry_stalled_jobs` task: the same proof passes |
| **procrastinate** itself | `finish_job` doesn't check the worker: a worker presumed dead finishes a job another worker has since fetched | — (a fencing token on finish) |
| **RQ** itself | A worker whose lease expired still settles the job after another worker took it: it marks it finished, or sends it back to be retried, while the new worker runs it | — |
| **RQ** itself | The reply to the write that records a job finished is lost, or its `on_success` callback raises: RQ fails the finished job and **runs it again** | — |
| **RQ** itself | A worker listening on two queues dies, or loses a reply, between popping a job and marking it started: **the job is gone**, queued in no queue and no registry | — |
| **Celery** itself | A worker's pool child dies after the task's SUCCESS is stored: the task stays SUCCESS and **its error callback fires beside its link** | — |
| **Celery** itself | A task's `on_success` hook raises after SUCCESS is stored: the same, both callbacks fire | — |
| **Celery** itself | The broker refuses the task's link after the body ran: the task is recorded FAILURE and acknowledged, so **the link is never sent** | — |

Demos are teaching code, and good at what they teach. The point is what the
harness finds when code like this is copied into production, and the one change
that makes the same proof pass. Every finding is a strict xfail with its
explanation, so an upstream fix turns it red. The full write-up:
[`demos/README.md`](https://github.com/gigaverse-app/due-work-harness/blob/main/demos/README.md).

**The findings, in 30 seconds**: Saleor's checkout run, its fix passing, then DBOS,
procrastinate and the coverage scan, all real output:

https://github.com/user-attachments/assets/ed065709-3372-40dd-8dc9-7f64dc60ef90

## Install

```bash
pip install due-work-harness            # the core: pytest and pydantic, nothing else
pip install "due-work-harness[django]"  # plus the Django/PostgreSQL integration
```

Or `uv add --dev due-work-harness`. The extras are `[django]`, `[celery]`,
`[procrastinate]`, `[dbos]`, `[redis]`, `[rq]`, `[mongodb]`, `[prefect]`, and
`[aiokafka]`; combine as needed. Python 3.12+.

## Coding-agent plugin

The [Due Work Harness plugin](plugins/due-work-harness/) helps Codex and Claude
Code bind your production workflow to a `DueWorkContract` and run its generated,
standardized pytest suite. The agent can then use the harness's crash histories,
lost-reply simulation, and other fault-injection primitives to add focused tests
for your application's own risks. Ask it, for example, "Why did this Celery task
disappear?", "Can this Shopify order be charged twice?", or "What happens if
this Kafka consumer crashes before committing its offset?" It traces the real
database-to-queue handoff, worker, or external API call rather than assuming
that a named framework guarantees durability. The plugin contains a skill, not
a remote service; the project under test still installs the pytest library.

For Claude Code, add this repository as a marketplace and install the plugin:

```bash
claude plugin marketplace add gigaverse-app/due-work-harness
claude plugin install due-work-harness@due-work-harness
```

The same plugin is packaged for Codex and can be submitted to the shared
ChatGPT/Codex directory. Execution needs a coding environment with the target
repository and its test services.

## Kill it on purpose: crash histories

Give the harness one transition that hands work off. It runs normal operation
to learn the outcome, then replays the same transition:

- with **every message it published lost**;
- with the worker **killed right after each commit** it makes;
- with the worker **killed right after each external call** returns;
- with **each after-commit callback failing**, **each signal receiver or job
  callback failing**, **the broker refusing each publication**, and **the reply
  to each commit lost** (the write lands, the worker sees a connection error and
  carries on), where your host supports them (the Django host breaks callbacks;
  the Redis host loses replies; add the Celery integration's breaker to refuse
  publishes).

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
due_work_harness.HistoriesDiverged: orders: handoff 'place order': normal operation reaches
('SENT', 1), but these histories reach something else: {'worker died after external call 1':
('SENT', 2)}. Work was lost or repeated. ...
```

That's the duplicate email, caught in a test instead of in a support ticket.

In a contract, a history declares its findings, and the generated case holds
the runs to that table and to the verdict at once. The histories run once, a
declared gap is a strict xfail for the divergence alone, and a finding that
moves fails as itself:

```python
HandoffHistory(
    name="place order",
    arrange=new_cart,
    transition=place_order,
    observe=...,
    findings=Findings(("SENT", 1), {"worker died after external call 1": ("SENT", 2)}),
)
```
When a test can't reach the worker at all (a workflow engine's own executor),
`process_histories` runs the real program in a child process, kills it at named
points with `os._exit`, restarts it the way production would, and applies the
same verdict. That is how the DBOS demo above works; a contract declares such
histories as `process_handoffs`, beside `handoffs`.

## DueWorkContract generates the standardized tests

Every domain adoption **must** use `DueWorkContract` and
`@due_work_contract_suite(CONTRACT)`; standalone histories or ordinary tests do
not complete adoption. See [the required adoption shape](ADOPTING.md#required-adoption-shape).
The decorator generates the suite at pytest collection time; you do not handwrite
one test per failure mode. A crash history proves one handoff. A declarative
`DueWorkContract` binds your production selection, tick and transitions, and
asks for a disposition (claim, decline with a reason, not applicable, or known
gap) for each of six lifecycle profiles:

| Profile | The question it answers |
| --- | --- |
| **A** automatic recovery | After a lost message, is the work still found? Does anything actually run the sweep, index-served and bounded? |
| **B** bounded ownership | Who owns the work right now, and what happens when that owner's lease expires? |
| **C** crash ambiguity | The provider never answered. Did the email go out? Schrödinger's email: can you tell "never ran" from "maybe ran"? |
| **D** durable retention | Can a cleanup pass delete work that is still owed? |
| **E** eventual convergence | When a result lands late, can it overwrite a newer one? |
| **F** fact-derived obligations | Can product state imply work nothing recorded, and is it still found? |

Every contract also disposes of two execution-safety profiles: replay safety and
bounded retry. Work that is owed but blocked by a product decision (a dependency
that has not settled, an owner still active) can declare an `ExecutionGate`
as `eligibility=`: blocked work must stay owed and untouched, and once eligible
it must complete by recovery alone, even with the readiness notification lost.
Read [what a green result means](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/what-a-green-result-means.md)
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

Cases are generated at pytest collection time, not written as Python files.
Use `--junitxml=<artifact-path>` for a machine-readable run artifact.
A contract class is empty in the source, so `pytest --due-work-summary` lists
what each one generated after the run: every case with its outcome, and each
declared gap's reason beside its strict xfail.

```text
test_wagtail_tasks.py::TestDjangoTasksDb: 7 passed, 5 xfailed
  XFAIL   A-known_gap  (the worker selects only READY tasks, so a task left RUNNING by a worker that died is…)
  PASSED  D-assert_retention_preserves_non_terminal_work
  ...
```

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

`uv run due-work-harness in-transaction` lists the handoffs made inside `transaction.atomic()`,
where the worker can run before the commit (and find no row) or after a rollback (and find a row that
never was). `on_commit` (and whatever is passed to it) and Celery's `*_on_commit` variants wait for the
commit and are not reported, nor are queues that can be a table in your own database (Procrastinate's
Django connector, django-tasks' database backend), where the job commits with the data. It is lexical, so
a handoff in a function the block calls is not seen. Each line is a place to read and write a contract
first, not a verdict: a publication that does not depend on what the block writes (a retry sent from a
read-only replica snapshot, say) is harmless.

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
| `[celery]` | beat-schedule evidence, a publication recorder that holds messages instead of sending them, a publication breaker that refuses one publish as a broker that is down would; and `celery_worker`: the application's real worker as a child process, failing at Celery's own stages (the pool child lost at `task_prerun`, `mark_as_done` and `task_postrun`, a raising `on_success` hook, a refused link), with `worker_contract()` for any adopter's task |
| `[procrastinate]` | its worker as recovery, "worker died holding this job" arrangement, the documented stalled-job recipe, periodic-task evidence |
| `[dbos]` | restarting an app through its own startup for process-level crash histories |
| `[redis]` | `redis_host()`: a commit counter for a queue kept in Redis (each pipeline or write command a commit, judged by the server's own command flags), and a reply breaker that lets a write land and loses its answer |
| `[rq]` | RQ's worker as the transition and as recovery (later workers' maintenance, with the clock moved on), its ownership bound to profile B, a breaker for job callbacks, and `worker_contract()`: RQ's whole contract for any adopter's jobs |
| `[mongodb]` | `mongodb_host(client, production_packages)`: acknowledged writes and transaction commits, worker death and lost replies on PyMongo 4.9–4.17; pass `motor_client.delegate` for Motor. See [MongoDB boundaries](docs/mongodb.md). |
| `[prefect]` | `prefect_flow_call(flow, runner)` completes the real flow body on one event loop; `assert_prefect_recurs` checks a declared deployment using Prefect’s schedule calculation. See [Prefect scope](docs/prefect.md). |
| `[aiokafka]` | `AIOKafkaConsumer` only: worker death after acknowledgement and lost commit replies, tested with broker restart/replay. See [aiokafka scope](docs/aiokafka.md). |

## Finding weaknesses in a project of your own or someone else's

The [upstream playbook](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/upstream-playbook.md)
is the cycle behind the findings above, written to be repeated: choose a target, map its handoffs, adopt
the harness in a fork, confirm each finding on its own, declare the findings, improve the harness with what
the probe needed, and disclose with the templates in
[`docs/upstream-templates`](https://github.com/gigaverse-app/due-work-harness/tree/main/docs/upstream-templates).
In Claude Code, the [`find-upstream-weaknesses` skill](https://github.com/gigaverse-app/due-work-harness/blob/main/.claude/skills/find-upstream-weaknesses/SKILL.md)
runs the cycle for you.

## Status

Alpha. The proofs were extracted from a production codebase, where they guard
its background workflows in CI; the public API may still change before 1.0.
Python 3.12+. How it's built: [ARCHITECTURE.md](https://github.com/gigaverse-app/due-work-harness/blob/main/ARCHITECTURE.md).

## Development

Managed with [uv](https://docs.astral.sh/uv/) (`uv sync --all-extras`), linted with Ruff and
type-checked with [Pyrefly](https://pyrefly.org/). See [CONTRIBUTING.md](https://github.com/gigaverse-app/due-work-harness/blob/main/CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](https://github.com/gigaverse-app/due-work-harness/blob/main/LICENSE).
