# Exercise application guarantees, not only assessment rows

Continue #36: give every shipped application the applicable profile proofs and
exercise real lifecycles before deciding a guarantee is absent. A profile with
no domain precondition remains inapplicable; a missing binding is not an
architectural reason to decline it.

- [ ] Replay every Wagtail media path and its CDN task; exercise failure exhaustion.
- [ ] Exercise Saleor payment evidence through the real mutation, including order,
      duplication, recovery, and interrupted admission; observe money independently.
- [ ] Exercise Saleor confirmation replay in both payment variants.
- [ ] Exercise DBOS's notification step directly as well as through process crashes.
- [ ] Exercise RQ's dependency gate through real Redis and worker execution.
- [ ] Revisit Procrastinate's follow-up and recovery assumptions with product observations.
- [ ] Strengthen executor examples and retain real worker/broker checks.
- [ ] Require the new behavioral evidence in CI; document exact tested and omitted shapes.
- [ ] Run framework suites, optional exploration, core regression tests, lint and types.

Findings must retain the failing invariant. Do not alter upstream applications or
substitute reference state machines to turn a newly discovered failure green.
