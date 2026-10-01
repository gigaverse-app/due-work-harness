# Exercise application guarantees, not only assessment rows

Continue #36: give every shipped application the applicable profile proofs and
exercise real lifecycles before deciding a guarantee is absent. A profile with
no domain precondition remains inapplicable; a missing binding is not an
architectural reason to decline it.

- [x] Replay every Wagtail media path and its CDN task; exercise failure exhaustion.
- [x] Exercise Saleor payment evidence through the real mutation, including order,
      duplication, recovery, and interrupted admission; observe money independently.
- [x] Exercise Saleor confirmation replay in both payment variants.
- [x] Exercise DBOS's notification step directly as well as through process crashes.
- [x] Exercise RQ's dependency gate through real Redis and worker execution.
- [x] Revisit Procrastinate's follow-up and recovery assumptions with product observations.
- [x] Strengthen executor examples and retain real worker/broker checks.
- [x] Require the new behavioral evidence in CI; document exact tested and omitted shapes.
- [x] Run framework suites, optional exploration, core regression tests, lint and types.

Findings must retain the failing invariant. Do not alter upstream applications or
substitute reference state machines to turn a newly discovered failure green.

Local validation: core 760 passed/1 skipped; Redis/Celery 431 passed/5 xfailed;
Prefect 202 passed; Procrastinate 60 passed/8 xfailed; DBOS 20 passed/2 xfailed;
Wagtail 54 passed/16 xfailed; Saleor with optional exploration 91 passed/13 xfailed.
Executed report checks distinguish verified guarantees from reproduced gaps.
Ruff and Pyrefly also pass in CI on commit `0241437`.
