---
name: find-upstream-weaknesses
description: Use to probe a real open-source project (an application first, or a framework) with due-work-harness, find background work that can be lost, repeated or misrecorded, and disclose it upstream as issues plus a PR that carries the harness contract. Triggers on "find problems in <project>", "probe <project>", "pick another app or library to test", "add a demo", "open an issue and a PR upstream", "disclose this finding", "adopt the harness in <project>", or any request to repeat the find-and-disclose cycle. Not for adopting the harness in the user's own repository (see ADOPTING.md).
---

# Find weaknesses in a real project, then disclose them

The full method is [docs/upstream-playbook.md](../../../docs/upstream-playbook.md); read the step you are
on when you reach it. The copy-and-fill templates are in
[docs/upstream-templates/](../../../docs/upstream-templates/). This file is the runbook: what to do, in
order, and the rules that are not negotiable.

## Rules that do not bend

- **Expose, don't fix.** Deliver a test that fails for a stated reason and a contract that finds it. Never
  propose or write the fix in an issue or PR.
- **Every PR carries the real contract**, with a plain test beside it; a plain test alone is not enough.
- **Claim only what you ran.** A sentence in a report is either measured or marked as code reading. Agents'
  findings (including your own from an earlier turn) are hypotheses until step 5.
- **Posting is outward-facing.** Issues, PRs, comments and releases go out under the user's account.
  Do it only with the user's authorization for this project in this session; if unsure, ask once, with
  the draft. Follow the target's own contribution rules over these: some ask for a human-written PR
  description or forbid AI-authored contributions, and then the user decides.
- **Every comment you post opens with the self-identification line**
  `> I'm {model}, AI can make mistakes. {a fresh 3-5 word joke}` (no joke on incident threads).
- **Every probe strengthens the harness.** A gap the probe hit becomes a general capability in the harness,
  released before the upstream PRs pin it.

## The cycle

Work in a scratch directory, not in a checkout that matters. Track the steps as tasks.

1. **Choose.** Prefer an application whose failure a customer or money feels; take a framework when apps
   depend on it and nobody has probed it. Count handoffs with `uv run due-work-harness check` (or the
   `scan` API), require that the project already recovers some of them, that its own tests run on this
   machine (Python floor, database, system packages), and that outside contributions are accepted. Read its
   CONTRIBUTING, templates and AI policy now. Search its tracker for prior reports. If two targets fit,
   pick one and say why; ask only if the choice needs the user's judgement (policy, risk to a relationship).
2. **Set up.** Fork and clone; put Postgres/Redis in containers the way their CI does; **run their
   unmodified suite first** so a red baseline is known before it is blamed on you.
3. **Map.** `due-work-harness sites` and `due-work-harness in-transaction` list the candidates first
   (a throwaway `[tool.due-work-harness]` table in the fork is enough). Then run the prompt in
   [map-the-handoffs.md](../../../docs/upstream-templates/map-the-handoffs.md) through an agent (fill the
   placeholders from the scan). Keep the result as ranked hypotheses.
4. **Adopt in the fork.** A branch such as `test/<what-breaks>`, a `tests/due_work/` directory named the way
   their runner collects it, a host (`django_host`, `redis_host`, or a new one), bindings that call the
   real code (a string command that reaches no project code is refused: pass the command class), a
   disposition for every profile with a reason, and one history per user-facing action whose observation
   includes what users and external systems saw. Use `--due-work-summary` to read the run.
5. **Confirm each finding.** Reproduce the smallest version with a script that uses the project's own API
   and no harness. Test the neighbouring claim before you make it, check server or library versions, and
   search the tracker again. Drop what you cannot reproduce or what the project defends.
6. **Declare and mark.** `Findings(delivered, {label: outcome})` on each history, `handoff_gaps` with the
   issue link, an `expectedFailure` plain test that fails for the stated reason (remove the marker once and
   read the failure), and the *this is where the magic happens* comment above each decorated class.
7. **Improve the harness.** If the probe needed something the harness lacks, or you wrote a helper twice,
   build it in the harness on its own branch (test it, in CI, on the stack it failed on), then run a DRY
   round from demo to application to integration to core. Release it (see RELEASING.md) and raise the pins.
8. **Disclose.** One issue per finding (template `issue.md`), then one PR (`pr.md`) that links the magic
   point at the pushed commit and shows the `--due-work-summary` capture, then the plain-language comments
   with ASCII art (`eli5-issue.md`, `eli5-pr.md`) on each. Fill their template and policy first.
9. **Keep it green.** Watch CI. The usual causes are in the playbook's table: lockfile, an environment that
   does not install the harness, a version-dependent table, a bot commit (merge, never rebase), workflows
   waiting for approval, a `main` that is already red (prove it, say so, leave their test alone).
10. **Record.** Update the demo and README tables if the project is a shipped demo, and keep the scratch
    material until the threads close.

## Before you say it is done

- [ ] their own suite ran green here first; the baseline is known
- [ ] each finding has an independent reproduction, and only measured claims are in the text
- [ ] contract, `Findings`, gaps, plain test, and the magic comment are in the pushed commit
- [ ] the harness gap (if any) is fixed, tested in CI on that stack, released, and the pins raised
- [ ] issues, PR and plain-language comments are posted with the self-identification line, no fix offered
- [ ] CI is green, or its red is explained with evidence
