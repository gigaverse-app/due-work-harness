# Demos: the harness against demos other projects ship

Each demo runs `due-work-harness` against a demo application **exactly as its
project publishes it**, pinned to a commit. Nothing upstream is vendored or
modified: `fetch_upstream.py` clones the pinned commits into `demos/.upstream/`.
The adapters bind the demo's own code — its views, tasks, workers and startup —
and replace only the slow or external call each demo already fakes (a
`time.sleep` standing in for network latency).

Demos are teaching code, and they are good at what they teach. The point is not
that they are wrong, but to show what the harness finds when code like this is
copied into production, and the one change that makes the same proof pass.

Each finding is a **strict xfail** carrying its explanation, next to a
**positive control** that passes with the fix. If upstream changes the demo, the
xfail turns red and the write-up here must change with it.

## Results

| Demo | Proof | As shipped | With the fix |
| --- | --- | --- | --- |
| [procrastinate `demo_django`](procrastinate_demo_django/) @ `35f3ca98` | Worker dies right after each commit of "create book" | **Finding.** The view commits the book, then defers `index_book` in a second autocommit statement; a death in between leaves the book never indexed, and nothing finds it again | Passes with `ATOMIC_REQUESTS = True` (the book and its job commit together) |
| | No transaction open during slow external work | **Finding.** `index_book` holds `@transaction.atomic` across its slow call | — |
| | A job whose worker died is picked up again | **Finding.** Nothing in the demo retries stalled jobs, so the job stays `doing` | Passes with procrastinate's documented `retry_stalled_jobs` periodic task |
| | A lost wake-up message | Not applicable, to procrastinate's credit: the queue is a table in the same database | — |
| [DBOS `transactional-outbox`](dbos_transactional_outbox/) @ `45a68c2`, dbos 3.1.0 | Process dies after the order commits, or before the notification is sent | Converges: restarting the app finishes the workflow | — |
| | Process dies after the notification is sent, before DBOS records the step | **Finding.** Recovery reruns the step and the customer is notified twice | Make the notification idempotent (a key the recipient deduplicates) |
| | An order whose process died before its workflow ran | Converges: `LossIsAbsorbedElsewhere` passes unmodified | — |

The DBOS finding is DBOS working as documented — an interrupted workflow
resumes from its last *completed* step — and the demo's "runs exactly once"
refers to the workflow, not to its external effect.

## Running them

```bash
python demos/fetch_upstream.py
pip install -e ".[django]" -e demos/.upstream/procrastinate "dbos==3.1.0" "fastapi[standard]>=0.115" time-machine
export PGUSER=postgres PGPASSWORD=postgres PGHOST=localhost PGPORT=5432
PGDATABASE=procrastinate_demo pytest demos/procrastinate_demo_django --ds=demos.procrastinate_demo_django.settings
pytest demos/dbos_transactional_outbox -p no:django
```

The DBOS demo creates and drops its own database (`dbos_outbox_demo`, override
with `DBOS_DEMO_DATABASE`).

## What each adapter binds

- **procrastinate:** the transition is the demo's `CreateBookView` through
  Django's test client; recovery is `manage.py procrastinate worker --one-shot`
  (`integrations.procrastinate.django_worker_once`). Production packages are
  `procrastinate` (the demo ships inside it) and `django`.
- **DBOS:** the transition is the demo's `create_order` endpoint in a child
  process started through the demo's own `main()`; recovery is `main()` running
  again in the test process (`integrations.dbos.restart_until`). Production
  packages are `dbos` and the demo's directory.
