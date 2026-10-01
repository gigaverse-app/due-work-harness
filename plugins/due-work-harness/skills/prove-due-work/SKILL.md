---
name: prove-due-work
description: Use due-work-harness to generate a standardized DueWorkContract pytest suite, and add focused fault-injection tests, when Python work could be lost, stuck, or repeated after a commit, worker crash, retry, redelivery, or uncertain API reply. For adoption in a project; not upstream disclosure, gigaverse-backend's internal harness, or ordinary unit-test debugging.
---

# Prove due work survives failure

Use this skill when a user asks why a background task vanished, why a worker repeated an effect, or whether a database-to-queue handoff, webhook, or retry is safe. The primary deliverable is a `DueWorkContract` whose decorator generates standardized pytest tests from real application bindings; use the same fault-injection infrastructure for additional focused tests when the domain needs them. The effect may be a payment, order, receipt, email, file, cache purge, import checkpoint, or downstream message. Work through the local repository; this skill does not call a hosted service.

For an external open-source project that the user wants to probe or report upstream, use [probe-upstream-due-work](../probe-upstream-due-work/SKILL.md). Before installing this package or applying its A–F lifecycle profiles, check for a repository-owned due-work harness or instructions. In gigaverse-backend, `.agents/skills/due-work/SKILL.md` and `docs/durable-work-conformance-harness.md` own the internal A–J contract and runtime policy; use those instead of this plugin's package-adoption workflow.

## Find the obligation

1. Trace the user-visible operation through production code. Identify the durable fact that creates an obligation, each database or queue commit, the external effect, and the actual recovery path. Search for dispatch, callbacks, scheduled work, retries, acknowledgements, and cleanup by behavior and domain name. Do not assume a familiar framework makes the workflow safe.
2. State the invariant in observable terms: what should exist after normal completion, what must eventually happen after a crash, and what must not happen twice. Observe external effects independently of state that recovery may erase.
3. Inspect existing tests and the project's test environment. Run its relevant baseline before attributing a failure to the harness. Use a disposable test database, broker, or provider stub as appropriate to the user's authorized environment. Do not exercise payments, messages, or other real external effects in production merely to prove a history.

## Map and test

4. Check whether `due-work-harness` is installed, which version and extras the project uses, and whether that release supports the project's Python version. If the user requested an implementation and the package is absent, add it as a development dependency with only the extras the project needs. Choose the integration by the concrete client and operation, using [integration boundaries](references/integrations.md). Where no adapter covers the boundary, use the framework-free callable host or identify the missing adapter; do not claim a proof from a simulated replacement for production behavior.
5. If the project has a `[tool.due-work-harness]` scan configuration, run `due-work-harness sites` and `due-work-harness in-transaction` with the project's environment. A scan result is a handoff candidate, not a bug. If configuration is absent, identify `production-packages` from the actual import roots before adding it. The scan covers specific Python handoff APIs; MongoDB writes, Prefect bodies, and Kafka offsets require their respective runtime bindings.
6. Build one small history for the real transition and real recovery. Inject at the supported commit, publication, callback, offset acknowledgement, or external-call boundary. Pair the interrupted history with normal operation and a control that would expose a missing or repeated effect. For process-level work, use the package's process histories where its worker or engine cannot be reached in-process.
7. For a complete adoption, declare `DueWorkContract` with truthful dispositions for lifecycle profiles A–F and both safety profiles, place histories in `handoffs=` or `process_handoffs=`, and expose a collected `@due_work_contract_suite(CONTRACT)` class. The decorator generates standardized pytest cases at collection time; do not handwrite copies of those cases. Bind transition, selection, and recovery to production code. A standalone history or focused custom test is useful for a domain-specific risk, but does not replace the contract. Follow the package's [adoption guide](https://github.com/gigaverse-app/due-work-harness/blob/main/ADOPTING.md) and [green-result limits](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/what-a-green-result-means.md) for the installed version.
8. Collect the generated cases, run them with `pytest --due-work-summary`, and use `--due-work-verify` for the completed coverage adoption. Record the command, version, relevant environment, passing cases, declared gaps, and the specific divergent history. A failing history is a finding only after checking the binding and, for an upstream disclosure, independently reproducing the result against the project's own API.

## Report the result

Explain the concrete lost or repeated effect, the exact failure point, what production recovery did, and the smallest test that demonstrates it. If all executed histories pass, state which boundaries were exercised and which remain outside coverage. Keep static scan candidates, code-reading hypotheses, tested findings, and known gaps distinct.
