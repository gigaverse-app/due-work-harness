# Find every handoff

A crash history proves one handoff. The static coverage check finds other
places in production code that hand work off, so a newly added task or queue
publication cannot quietly escape review:

```bash
due-work-harness check
```

It scans for calls such as Django `on_commit`, Celery `.delay`, procrastinate
`.defer`, DBOS workflow starts, Dramatiq `.send`, RQ `enqueue`, and Django
task `.enqueue`. It attributes each to the function that makes the handoff and
requires one disposition: a collected contract that covers it, or an
exemption proving that losing it is harmless. Aliases, re-exports, and
references passed along uncalled still count. In the Saleor demo it finds 137
handoff sites in 124 functions.

```python
@due_work_contract_suite(ORDER_NOTIFICATIONS, covers=(DueWorkSource(OrderService.place),))
class TestOrderNotificationsDueWork:
    pass
```

A declaration counts only if pytest would collect it;
`pytest --due-work-verify` checks it actually ran. Existing projects can start
with a baseline that only shrinks. [The adoption guide](../ADOPTING.md#2-configure-the-scan)
has the configuration, supported site kinds, and CI actions.

`due-work-harness in-transaction` also lists handoffs made lexically inside
`transaction.atomic()`: a worker may run before the commit or after a rollback.
`on_commit`, Celery's `_on_commit` variants, and database-backed queues whose
jobs commit with product data are not reported. This is a place to investigate,
not a verdict: the publication might not depend on the transaction's writes,
and calls inside another function are outside this lexical scan.

In GitHub Actions, `gigaverse-app/pytest-obligation/check@v0` runs the static
check and `gigaverse-app/pytest-obligation/test@v0` runs generated suites.
