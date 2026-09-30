# Demos: the harness against code other projects ship

Each demo runs `due-work-harness` against a demo application, or a real
open-source application, **exactly as its project publishes it**, pinned to a
commit. Nothing upstream is vendored or
modified: `fetch_upstream.py` clones the pinned commits into `demos/.upstream/`.
The adapters bind the demo's own code — its views, tasks, workers and startup —
and replace only the slow or external call each demo already fakes (a
`time.sleep` standing in for network latency).

Demos are teaching code, and they are good at what they teach. The point is not
that they are wrong, but to show what the harness finds when code like this is
copied into production, and the one change that makes the same proof pass.

Each demo **adopts the harness the way a project does**:

- a `pyproject.toml` with `[tool.due-work-harness]`, pointing the coverage
  check at the pinned upstream, and a baseline for the handoffs no contract
  insures yet: `due-work-harness check --root demos/<demo>` passes, and CI runs
  it through the `check` action;
- a `DueWorkContract` for the obligation, with a disposition for every profile
  and both safety profiles, its handoffs and their delivery, decorated with
  `@due_work_contract_suite(..., covers=(DueWorkSource(<the site>),))`. What
  the harness finds is declared as legacy gaps (`KnownGap`, `handoff_gaps`, an
  `ExtraProof`'s `gap`), each generated as a **strict xfail** carrying its
  explanation;
- a second contract with the fix, as the **positive control**, which passes.

If upstream changes a demo, a strict xfail turns red and the write-up here must
change with it.

The [executed capability map](../docs/adopter-capabilities.md) records the full
profile scope, including payment-report evidence and crash/race findings,
automatic-crop supersession, per-effect replay and retry, and follow-up admission.

A new demo, or a report to the project a demo found something in, follows the
[upstream playbook](../docs/upstream-playbook.md).

Where the framework has a capability, the contract **claims** it and the harness
proves it against the framework itself, through its integration
(`due_work_harness.integrations.procrastinate`, `.dbos`); a decline or "not
applicable" is kept only where the framework genuinely lacks the capability,
with the reason. Findings in the framework, not the demo, appear in both
contracts, since the demo's fix cannot change them.

| Demo | Claimed and proven against the framework | Found (strict xfails) | Covers | Fix contract |
| --- | --- | --- | --- | --- |
| procrastinate | B ownership (its workers, heartbeats and reclaim recipe), D retention (`remove_old_jobs`), replay safety, bounded retry | **in procrastinate:** `finish_job` does not check the worker, so a worker presumed dead finishes a job another worker has since fetched; **in the demo:** the create-book handoff, a transaction held during slow work, no stalled-job reclaim scheduled, no attempt recorded before the slow call (C) | `CreateBookView.form_valid` | `DEMO_WITH_ITS_FIXES`: `ATOMIC_REQUESTS`, and procrastinate's `retry_stalled_jobs` recipe |
| DBOS | D retention (`garbage_collect`), bounded retry (the step's budget), recovery through real process deaths | a death after the send, before DBOS records the step, notifies twice (C, replay) | nothing: its enqueue is SQL, which a static scan cannot see | — |
| Wagtail on django-tasks-db | D retention (`prune_db_task_results`), claimed so the harness proves it | **in django-tasks-db:** a `task_finished` receiver that raises rewrites a task that ran as FAILED, a `task_started` one fails it before it runs, and a task whose worker died stays RUNNING forever (A, B, C); **in Wagtail:** deleting an image or document can orphan its file in storage, publishing can leave the CDN serving the old page, and nothing sweeps for either (A, F) | the two `post_delete_file_cleanup` handlers and `purge_urls_from_cache` | — |
| Saleor | A recovery (automatic completion: 13 proofs, its selection observed from the tick it runs rather than restated), D retention (`delete_expired_checkouts`), claimed so the harness shows where each fails | automatic completion dispatches a paid checkout again while its completion is still in flight, and reports no backlog (A); `delete_expired_checkouts` deletes a checkout holding a captured Payments API payment (D); no sweep recovers lost after-commit work (A); the capture is never reconciled (C); nothing records that a confirmation is owed (F); the complete-checkout handoff | `_post_create_order_actions` (its two `on_commit` sites) | `CHECKOUT_WITH_AUTOMATIC_COMPLETION`: the Transactions API with automatic completion, which claims D and passes |

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

## Real applications

| Application | Proof | As shipped | Saleor's own answer |
| --- | --- | --- | --- |
| [Saleor](saleor_checkout/) @ `5ff56489`, `complete_checkout` | Worker dies right after each commit, then Saleor runs every periodic task it schedules for 91 days | **Finding 1.** With the Payments API (payment plugins), the payment is captured in its own transaction before the order's. A death in between charges the customer and never creates the order; 90 days later Saleor deletes the checkout and the captured payment belongs to nothing | With the Transactions API and `automatically_complete_fully_paid_checkouts`, the same deaths always end with an order: Saleor's beat task completes the paid checkout |
| | | **Finding 2.** Everything after the order commits (its events, the ORDER_CREATED webhooks, the confirmation) runs in `on_commit` callbacks, each write committing on its own. A death there leaves a paid order never confirmed, its history empty or half-written, and nothing Saleor schedules re-runs it | — |
| | Each after-commit callback fails, the process alive | **Finding 3.** When `order_created` raises (a webhook payload bug, a plugin error, a database error), Django skips every later callback of the commit: the customer is not sent their confirmation | — |
| | The broker refuses each publication in turn, the process alive | **Finding 4.** With an integration subscribed to the order's webhooks, the broker refusing any one of them (ORDER_CREATED, ORDER_FULLY_PAID or ORDER_CONFIRMED) escapes `order_created`: the webhooks and history after it are never written, Django skips the confirmation, and completing the checkout raises for a placed, paid order | — |

Saleor's own test settings, fixtures and code run unchanged: the demo builds
Saleor's environment from its `uv.lock`, loads its root `conftest.py`, and
arranges each checkout the way Saleor's own `checkoutComplete` tests do. What
each of the fourteen histories costs is pinned in
[`test_order_confirmation.py`](saleor_checkout/test_order_confirmation.py)
(`FINDINGS`), so a change upstream or in the harness shows exactly what moved.
Two things to keep in mind when reading the findings: a customer who retries
`checkoutComplete` within the 90 days does get the order, and deaths *after* a
payment is captured are rare. Finding 3 needs no death at all.

| [Wagtail](wagtail_tasks/) 8.0 on django-tasks-db 0.13, image and document deletion | Worker dies right after each commit of Wagtail's admin delete view, and its after-commit callback fails, then `db_worker` runs | **Finding.** The view commits the row, then enqueues `delete_file_from_storage_task` from `transaction.on_commit`: a death in between, or a failing enqueue, leaves the file in storage with nothing to delete it. Served straight from storage, a deleted original stays reachable at its URL | — |
| Wagtail publishing, behind a CDN | Worker dies after each commit of a publish, and `page_published`'s receiver fails | **Finding.** Publishing commits the page, then enqueues the purge in a later write: a death between them, or the frontend cache's receiver raising, leaves the new content live and the old page cached | — |
| django-tasks-db's `db_worker` running Wagtail's purge | Worker dies after each commit and after the CDN call; each `task_started` and `task_finished` receiver fails | **Finding.** A `task_finished` receiver that raises after the task ran rewrites its SUCCESSFUL record as FAILED, since `run_task` sends it inside the `try` that records failures; a raising `task_started` receiver fails the task before it runs. A task whose worker died stays RUNNING forever, a gap django-tasks-db already tracks | — |

The Wagtail demo runs Wagtail's own test project from the wagtail wheel, with
Postgres, django-tasks-db's database backend and a recording CDN. Its
migrations seed the root page, site and collection, so its host restores them
after each committing case (`django_host(serialized_rollback=True)`); what each
of its 27 histories costs is pinned in
[`test_wagtail_tasks.py`](wagtail_tasks/test_wagtail_tasks.py).

## Running them

```bash
python demos/fetch_upstream.py
pip install -e ".[django]" -e demos/.upstream/procrastinate "dbos==3.1.0" "fastapi[standard]>=0.115" time-machine
export PGUSER=postgres PGPASSWORD=postgres PGHOST=localhost PGPORT=5432
PGDATABASE=procrastinate_demo pytest demos/procrastinate_demo_django --ds=demos.procrastinate_demo_django.settings
pytest demos/dbos_transactional_outbox -p no:django
due-work-harness check --root demos/procrastinate_demo_django
due-work-harness check --root demos/dbos_transactional_outbox
```

The DBOS demo creates and drops its own database (`dbos_outbox_demo`, override
with `DBOS_DEMO_DATABASE`).

The Wagtail demo is its own pytest rootdir, so that `--due-work-verify` reads its
coverage table; Wagtail's source is fetched only for the coverage check:

```bash
python demos/fetch_upstream.py wagtail
cd demos/wagtail_tasks && PGDATABASE=wagtail_tasks pytest --create-db --due-work-verify
due-work-harness check --root demos/wagtail_tasks
```

The Saleor demo runs in Saleor's own environment, built from its `uv.lock` on
Linux (Saleor's development dependencies do not build on Windows):

```bash
python demos/fetch_upstream.py saleor
DATABASE_URL=postgres://postgres:postgres@localhost:5432/saleor demos/saleor_checkout/run.sh -rxX --due-work-verify
demos/saleor_checkout/run.sh --typecheck
due-work-harness check --root demos/saleor_checkout
```

Creating Saleor's test database runs all of its migrations, about a minute and a
half. To run on several xdist workers, migrate once and give each worker a copy,
as CI does:

```bash
demos/saleor_checkout/run.sh --prepare-db 4
demos/saleor_checkout/run.sh -rxX --due-work-verify -n 4 --reuse-db
```

## What each adapter binds

- **procrastinate:** the transition is the demo's `CreateBookView` through
  Django's test client; recovery is `manage.py procrastinate worker --one-shot`
  (`integrations.procrastinate.django_worker_once`). Production packages are
  `procrastinate` (the demo ships inside it) and `django`.
- **DBOS:** the transition is the demo's `create_order` endpoint in a child
  process started through the demo's own `main()`; recovery is `main()` running
  again in the test process (`integrations.dbos.restart_until`). Production
  packages are `dbos` and the demo's directory.
- **Saleor:** the transition is `complete_checkout`, called as the
  `checkoutComplete` mutation calls it. Not through the GraphQL view: its
  graphql-core 2 executor waits on a promise that a simulated death (a
  `BaseException`) never resolves, where a real death never reaches the
  resolver. Recovery is every task in Saleor's `CELERY_BEAT_SCHEDULE`, at one
  hour, one day, 31 and 91 days. Profile A binds `due_work` to the query
  Saleor's own `trigger_automatic_checkout_completion_task` evaluates, captured
  as it runs with its dispatches held
  (`integrations.django.selection.selection_built_by`), because Saleor builds
  that selection inline: observing it proves Saleor's query, where restating it
  would prove a copy. Confirmations are counted at
  `PluginsManager.notify`, the seam Saleor's own tests mock. Production
  packages are `saleor` and `django`.
