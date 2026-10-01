# A working adopter

Run the application contracts from this directory:

```sh
uv sync --locked
uv run pytest --due-work-verify --due-work-require-assessed \
  --due-work-profile-report=profiles-example.json
```

[Catalog](adopter_app/catalog.py) is a small file-backed SQLite application. It
commits publication intent and an obligation together, waits for editor approval,
publishes desired revisions, retains retirement tombstones, reconciles uncertain
remote writes, and merges delivery receipts by sender attempt. Its constructor
accepts external provider and notification ports. It is ordinary application
code, independent of the harness.

[test_bindings.py](test_bindings.py) connects it to the harness. Assertions and
schedules come from the library. Each history creates its own SQL file, provider,
notifications and receipt feed; cleanup happens after assertions. The same
bindings run through RQ's real Redis queue/SimpleWorker, Celery's eager task
tracer and Prefect's async flow-body adapter in the integration suites. Those
variants prove application behavior through those execution paths. Separate
existing worker suites prove process crashes and queue recovery.

The generated catalog covers:

- G: approval blocks execution without losing or rewriting intent; recovery finds
  approval even though the readiness notification is never delivered.
- H: replay actually calls the provider twice and leaves the same remote value.
- I: admission owns its transaction; failure after both SQL writes leaves neither
  committed, and notifications only leave after commit.
- E in-flight: held, refused and lost provider responses; success replies that
  never applied; three revisions and return to an earlier value; every
  completion order; independent progress; retirement and failed repair; lost
  and duplicate notifications.
- E evidence: partial receipts, permutations, duplicate receipts, batch partitions,
  separate actor connections (inline executor), and old receipts replayed during
  a new sender turn. Prior-attempt evidence remains immutable.

Profiles outside this application's scope and the two other E families have
explicit reasons in its contract. For example, the example has no periodic
scheduler, leases, pruning policy or retry limit. It does not claim those guarantees.

## Optional exploration and reproducible failures

```sh
uv sync --locked --extra exploration
uv run pytest --due-work-explore=smoke --due-work-require-assessed \
  --due-work-profile-report=profiles-example.json
```

Normal fixed histories and trace replay require no Hypothesis. `smoke` explores
20 schedules per scenario; `deep` explores up to 200. These are extra schedules
inside generated pytest cases, not hundreds of separately collected tests.

[test_catalog_failures.py](tests/test_catalog_failures.py) deliberately disables
remote-drift detection and destroys previously committed receipts. The generated
histories catch both defects, serialize the failure's `HistoryTrace`, then replay
it against fresh SQL state and require the same invariant to fail. These are
sensitivity controls, not claims of new upstream bugs. They also illustrate how
to replay a failing JSON trace without Hypothesis.

[summaries.py](adopter_app/summaries.py) remains the separate minimal exemption
example: losing its cache invalidation costs nothing because reads rebuild the
summary. Both examples run through the published check/test GitHub Actions.
