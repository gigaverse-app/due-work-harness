# Prefect flow bodies and recurrence

Install `pytest-obligation[prefect]` for Prefect 3.7.

`prefect_flow_call(flow, runner)` turns a real `prefect.Flow` into a synchronous
history binding. It executes `flow.fn`, awaits the entire coroutine on the
caller-owned `asyncio.Runner`, and retains the production function's identity for
binding checks. Reuse the same runner for loop-bound clients such as Motor.

This proves application recovery through the flow body. It does not run Prefect's
engine, task retries, hooks, result persistence or remote worker delivery. Test
those separately with the actual engine. A synchronous flow body is also supported;
returned generators and non-coroutine awaitables are rejected as unfinished work.

`assert_prefect_recurs(deployment, flow, start=..., within=..., runner=...)` checks a
real `RunnerDeployment`: the flow and entrypoint must match, it must be unpaused,
and active schedules must provide repeated dates within the requested interval.
Date calculation uses Prefect's own server schedule models, including timezone
semantics. This is evidence about the supplied declaration, not the state of a
remote deployment or whether a worker is alive. Bind the actual declaration your
application registers; reconstructing its cron in the test would miss drift.

The tests pair working bindings and schedules with paused, inactive, missing,
slow, wrong-flow and wrong-entrypoint controls.

The binding rejects deferred results from both synchronous and asynchronous flow
bodies. Awaiting the outer coroutine is insufficient when it returns a generator
or another awaitable. Schedule evidence checks the first **three future** runs,
excluding a tick exactly at `start`; it is a bounded declaration check, not proof
that an RRule continues forever or that a deployed worker executes those runs.

A rejected asyncio task/future belonging to the supplied runner is cancelled and
drained before the binding raises, so it cannot resume when the next history
uses that loop. Unrelated caller tasks and futures on other event loops remain
caller-owned. This is not a whole-loop or whole-process termination guarantee.

## Required adoption surface

Bind this flow callable and schedule evidence through a `DueWorkContract` with
a collected `@due_work_contract_suite(CONTRACT)` class. Put histories in
`handoffs=` and claim only profiles for which production bindings exist.
Calling the adapter in ordinary tests alone is incomplete domain adoption; see
[required adoption shape](../ADOPTING.md#required-adoption-shape) for generated
cases and report artifacts.
