---
name: probe-upstream-due-work
description: Use pytest-obligation to investigate an external open-source Python project's lost or duplicate background work, verify a concrete failure, and prepare an upstream issue or test-only PR when requested. Not for adopting the harness in your own application, ordinary code review, or gigaverse-backend's internal due-work harness.
---

# Probe due-work failures upstream

Use this skill when the user asks to find due-work bugs in another project or to disclose a verified finding. For adoption in the user's own project, use [prove-due-work](../prove-due-work/SKILL.md). If the target has its own due-work instructions, read and follow them before choosing a harness; gigaverse-backend has a separate internal harness and [repo-local skill](https://github.com/gigaverse-app/gigaverse-backend/blob/main/.agents/skills/due-work/SKILL.md).

The [upstream playbook](https://github.com/gigaverse-app/pytest-obligation/blob/main/docs/upstream-playbook.md) owns the detailed cycle and [disclosure templates](https://github.com/gigaverse-app/pytest-obligation/tree/main/docs/upstream-templates). Read the relevant step when you reach it. This skill packages the portable decision points, not a second copy of that runbook.

1. Choose an application or framework with consequential background work, a runnable test environment, and a recovery mechanism worth probing. Check contribution and AI policies, issue history, and the project's baseline tests before attributing a failure to it. Keep experiments in an isolated fork or scratch checkout.
2. Map each candidate from the user-visible action to its durable fact, database/queue handoff, external effect, and actual recovery. `due-work-harness sites` and `in-transaction` can surface handoffs when configured; their output is a candidate list, not proof of a bug.
3. Bind the project's real transition and recovery to a `DueWorkContract` and a collected `@due_work_contract_suite(CONTRACT)` class. Give each required profile an honest disposition. Use real services or faithful test doubles and observe effects independently of cleanup. Follow [prove-due-work](../prove-due-work/SKILL.md) for the package's supported boundaries and generated-case workflow.
4. Execute the generated cases and record the environment, versions, normal result, divergent history, and `--due-work-summary`. Independently reproduce each proposed finding through the project's own API without the harness; check neighboring conditions and prior reports. Drop claims that do not survive this check.
5. If the user requests an upstream report, prepare a minimal plain-language reproducer and a test-only contract PR using the project's contribution rules and the playbook templates. Separate passing proofs, declared gaps, and untested hypotheses. Leave the production fix to maintainers unless the user separately asks for it.

Issues, PRs, comments, releases, and other outward-facing changes require the user's authorization for that project; investigating a project does not by itself authorize posting. Do not change the harness, publish a release, or add a dependency to another repository merely because a probe would benefit from it. Explain the missing capability and request scope before expanding the work.
