# Where the shipped adopters exercise the capabilities

This is the adoption map for the generated capabilities ported in #34. CI runs
these contracts with `--due-work-require-assessed` and uploads executed JSON
profile reports. CI also checks required adopter names and executed profiles
against those reports, so deleting a suite cannot leave an empty green report. The option rejects assessment debt before `-k`/marker filtering;
reports distinguish declarations, selected cases, actual outcomes and verification.
A Decline or known gap remains unverified. A report covers collected suites only.

| Adopter | New executable adoption | Scope and remaining guarantees |
| --- | --- | --- |
| [SQLite catalog](../examples/adopter/README.md) | G/H/I; E revisions, retirement, transport, receipts, batches, separate connections, retry turnover; optional Hypothesis and JSON replay | Full demonstration of the new engines on persisted application state; no periodic scheduler or lease claim |
| RQ integration | Same application bindings through Redis + SimpleWorker; H on the existing message job | Message replay exposes duplicate sends as a strict gap; existing ownership/retry/crash findings remain pinned |
| Celery integration | Same application bindings through the eager task tracer; H on the existing message task | Deterministic interleavings do not claim broker scheduling; the existing prefork process suite checks actual worker deaths and callbacks |
| Prefect integration | Same bindings through the actual async flow-body adapter | Coroutine completion and application invariants, not Prefect engine retries or deployment delivery |
| Django integration | I against real PostgreSQL atomic and nested transactions, split-commit negative controls | Shared SQL-boundary fault adapter never supplies a transaction or rollback |
| Procrastinate Django demo | I on the real create-book view | Split admission fails; the existing ATOMIC_REQUESTS variant passes. Existing H/J/B/D remain executable; no product readiness gate or mutable-result merge |
| DBOS transactional outbox | I on real order INSERT + SQL workflow enqueue | Interruption observes both uncommitted writes on the actual connection, then verifies rollback outside it. Existing process deaths, D and J remain; no post-admission gate |
| Wagtail media | I on both image and document deletion | Product/task split commits remain strict gaps; deletion is immediately eligible |
| Wagtail publishing | I on page publication and H on the real CDN purge task | Split commit is a strict gap; repeated invalidation is harmless and measured independently from call count |
| Saleor checkout, both payment variants | Executable I gap probes with real crash histories and confirmation observations | After-commit obligations lack durable admission. Automatic completion protects the paid order but still loses confirmation. No invented obligation before payment |
| MongoDB/Motor, aiokafka | Existing real-server driver/offset fault controls continue in CI | These are host/protocol controls, not application contracts. A driver cannot supply product intent, desired revisions, evidence or approval predicates; the application supplies those bindings |

Framework worker factories explicitly decline application-owned admission and
gating instead of inheriting unexplained `NotAssessed` placeholders. This does
not assert that RQ dependencies, Celery canvases or arbitrary user tasks are safe.
An application's contract can compose/override the worker declaration with its
own bindings, as the RQ and Celery message replay suites do.

G can be used with native recovery without claiming an A-style sweep. If A is
claimed, the additional G/A proof still requires the gate's recovery to dispatch
its identity through the contract's actual sweep. Existing negative controls
continue to reject an unrelated recovery implementation.

The upstream examples do not have mutable remote revisions or a partial-evidence
merge API where one is absent. Their concrete E reasons remain explicit; the
catalog demonstrates those scenarios rather than binding a reference state
machine and calling it an upstream guarantee.
