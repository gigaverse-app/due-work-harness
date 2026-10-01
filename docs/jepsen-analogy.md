# Coming from Jepsen: fault testing for application work

If you work on distributed systems and know Jepsen, pytest-obligation should
feel familiar: execute real operations, introduce failures, observe what
happened, and check whether the result respects the system's promises.
It applies that approach to the work your application owes—orders, payments,
receipts, background tasks, imports, and downstream effects.

## The correctness gap above the database

A database can preserve every committed transaction while your application
still loses an order. Consider this workflow:

1. Capture the customer's payment.
2. Commit the order.
3. Publish its confirmation task.
4. Send the receipt and record completion.

A worker death between steps 1 and 2 can leave a charge without an order. A
lost publication after step 2 can leave an order without its confirmation. A
death after sending the receipt but before recording completion can make
recovery send it twice. Database consistency alone cannot establish that the
whole workflow finishes correctly.

pytest-obligation binds the real transition, observable effects, and production
recovery path to a `DueWorkContract`. It generates standardized tests that
challenge those boundaries and check what remains after recovery. The
[Saleor and DBOS demonstrations](../demos/README.md) reproduce concrete
examples of these failures against pinned upstream code.

## How the approaches relate

[Jepsen](https://github.com/jepsen-io/jepsen#design-overview) combines workload
generators, clients, a fault injector called a *nemesis*, operation histories,
and correctness checkers. Its Clojure test programs commonly orchestrate
distributed clusters and test behavior during crashes, partitions, and other
faults. Its scope also includes queues and task schedulers.

| Concept | pytest-obligation counterpart |
| --- | --- |
| Workload and client operations | Production transitions and application commands bound by the adopter |
| Fault injection | Supported commit deaths, lost publications/replies, callback failures, external-call faults, and controlled competing operations |
| Recorded history | Observed crash outcomes and interleaving traces |
| Correctness checker | Generated A–J profile tests and application invariants |
| Analysis and reproducer | Divergent outcomes, strict known-gap cases, executed profile reports, and supported trace replay |

This is a methodological analogy. pytest-obligation is an independent Python
project; it does not use Jepsen as its engine or imply affiliation. It provides
application contracts and pytest integration rather than Jepsen's general
cluster orchestration and database consistency analysis. In particular, its
controlled interleavings exercise declared application boundaries; they do
not explore every CPU instruction, SQL schedule, or network partition.

## What you get from the contract

The A–J catalog supplies recurring correctness questions: recovery after lost
notifications, bounded ownership, uncertain external effects, retention,
convergence, discovery of unrecorded obligations, execution gates, harmless
replay, atomic admission, and retry limits. You bind the application behavior;
the harness supplies the standard tests, so each adopter need not design that
catalog from scratch.

A pass is evidence about the executed cases, declared boundaries, observations,
and recovery budget. Known gaps and unfinished assessment remain visible.
Read [what a green result means](what-a-green-result-means.md) and
[generated interleavings and replay](interleavings.md) for those limits.

## Try it on one workflow

Start with a workflow that must finish after a crash and must not repeat an
external effect. In Claude Code or Codex, with the pytest-obligation skill
installed, ask:

> Use `prove-due-work` to create a `DueWorkContract` for our order workflow.
> Bind production admission, payment/receipt effects, and recovery. Generate
> and run crash, replay, and convergence tests, and show the exact history
> behind each finding and the guarantees that remain unassessed.

Follow the [adoption guide](../ADOPTING.md) for manual setup and the
[pytest plugin guide](pytest-plugin.md) for running the generated suite in CI.
