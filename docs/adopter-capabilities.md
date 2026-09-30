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
| RQ integration | Same application bindings through Redis + SimpleWorker; H on the existing message job; G with one and two real dependencies | Message replay exposes duplicate sends as a strict gap; existing ownership/retry/crash findings remain pinned |
| Celery integration | Same application bindings through Redis and a real Celery solo worker; H on the existing message task | Deterministic interleavings do not claim broker scheduling; the existing prefork process suite checks actual worker deaths and callbacks |
| Prefect integration | Same bindings through the actual Prefect flow engine and isolated API server | Flow execution and application invariants; deployment delivery remains outside this local test |
| Django integration | I against real PostgreSQL atomic and nested transactions, split-commit negative controls | Shared SQL-boundary fault adapter never supplies a transaction or rollback |
| Procrastinate Django demo | I on the real create-book view; commit-death histories for index_book's follow-up admission | Split admission fails; the existing ATOMIC_REQUESTS variant passes. Existing H/J/B/D remain executable; no product readiness gate or mutable-result merge |
| DBOS transactional outbox | I on real order INSERT + SQL workflow enqueue; H on the actual notification step | Atomic admission passes; replaying the step duplicates the notification. Existing process deaths, D and J remain; no post-admission gate |
| Wagtail media | I on image/document deletion; H/J separately for image, document and feature detection; E on stale crop calculation | Product/task split commits and stale crop overwrite remain strict gaps; deletion is immediately eligible |
| Wagtail publishing | I on page publication; H/J on the real CDN purge task | Split commit is a strict gap; repeated invalidation is harmless; persistent CDN failure exhausts execution |
| Saleor checkout, both payment variants | H confirmation replay and executable I gap probes with real crash histories and confirmation observations | After-commit obligations lack durable admission. Automatic completion protects the paid order but still loses confirmation. No invented obligation before payment |
| Saleor payment reports | E charge/refund evidence, duplicate delivery, recovery, separate actors and optional Hypothesis; I commit interruption; overlapping request regression | Sequential evidence histories pass. Interrupted projection and overlapping aggregate writes leave incorrect money even after redelivery |
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

Application-specific E bindings cover Wagtail's stale snapshot and Saleor's
payment evidence. Other E families retain explicit reasons where the application
has no corresponding lifecycle. The catalog demonstrates the remaining generated
scenarios without claiming they prove an upstream application's behavior.

## Deeper application coverage

The follow-up extends executable guarantees, not just assessment rows:

- RQ dependency gates: one and two prerequisites, with real Redis job-write observation.
- Wagtail media: separate image/document/automatic-crop replay and service-failure limits;
  stale queued automatic crops versus newer editor choices (E).
- Wagtail publishing: real worker exhaustion when the CDN is unavailable (J).
- DBOS: actual notification-step replay (H), alongside existing real process-death histories.
- Procrastinate: index_book's follow-up admission now has commit-death histories and
  leaves the static scanner baseline.
- Saleor: both confirmation callbacks now have replay proofs; payment-event reports
  exercise charge/refund permutations, duplicate delivery, recovery and separate actors.
  Commit interruption and genuinely overlapping requests independently check the money projection.

Newly reproduced failures: Saleor retains a charge event but leaves the transaction
amount zero after a crash before projection; overlapping reports can overwrite a
newer aggregate, and identical redelivery repairs neither. Wagtail's queued detector
can overwrite a manual crop chosen after enqueue. These are strict expected failures
against the pinned upstream versions, not claims that the applications pass.

Named `replay` and `retry` bindings run every independent effect through the same
profile proofs. A passing image-delete test cannot stand in for document deletion
or automatic crop calculation. The existing singular factory form remains supported.
