# Finding weaknesses in real projects: the playbook

How to point the harness at a project you don't own, find a weakness that costs someone something, and
disclose it so the maintainers can reproduce it in minutes. It is the cycle behind the findings in the
[README](../README.md#not-theoretical-it-found-these-in-code-youve-heard-of) and the
[demos](../demos/README.md), written so a person or an agent can run it again.

The rules that shape every step:

- **Expose, don't fix.** The deliverable is a test that fails for a stated reason, and a contract that
  finds it. A fix is theirs to shape.
- **Every PR carries the real contract**, not only a plain test. The plain test proves one bug; the
  contract shows the project how to find the next one.
- **Every probe strengthens the harness.** When a probe needs something the harness lacks, that goes into
  the harness first (steps 6 and 8), as a general capability, not as a hack in the fork.
- **Claim only what you measured.** Each sentence in a report is either something you ran or is marked as
  code reading.

## 1. Choose the target

Prefer an **application** over a framework: an application's failures are ones a customer, an
organiser or money feels (a paid order with no ticket), which is what makes maintainers read the report.
Take a framework when the applications on top of it depend on it and no one has probed it (RQ and Celery),
or to find the cause of an application finding (django-tasks-db came from Wagtail).

Good targets:

- hand work off after a commit somewhere the user notices; `uv run due-work-harness check` (or the
  `scan` API) counts the handoffs and the functions that make them. Saleor has 137 sites in 124 functions,
  pretix 78 sites in 69;
- recover *some* of it already (a periodic sweep, a retry table). A project with no recovery makes a weak
  test; one with some makes the question "which handoffs are missing it?" fair;
- run their own tests on a machine you have: check the Python floor, the database, system packages;
- accept outside contributions. Read `CONTRIBUTING`, the issue and PR templates and any AI policy
  **before** writing anything. Wagtail, for one, asks that a person write the PR description.

Check for existing reports before you invest: `gh issue list --repo OWNER/REPO --search "<keywords>"
--state all`.

## 2. Set up

Work in a scratch directory, never in a checkout you care about.

```bash
gh repo fork OWNER/REPO --clone        # their code, your remote
git -C REPO checkout -b test/<what-breaks>
```

- Run the databases and brokers the project's own CI runs (Postgres, Redis, RabbitMQ) in containers,
  and run **their unmodified test suite first**. A red baseline (Wagtail's `main` had one) has to be known
  before it is blamed on you.
- Use the harness from source (`PYTHONPATH=<harness>/src`) while it changes, and from PyPI once released.
- Keep Docker networking simple: `--network container:<the database container>` lets the test process
  reach `localhost:<port>` exactly as their CI does.

## 3. Map the handoffs

Read the code from the user's side: place the order, pay it, send the mail. For each step note what
commits together, what is published after the commit (`on_commit`, `.delay`, `apply_async`, a signal), and
**what recovers it if the process dies right after, or the broker refuses the publish**. The map that
finds weaknesses is a recovery map: durable record, periodic recovery, none.

An agent does this well. [`upstream-templates/map-the-handoffs.md`](upstream-templates/map-the-handoffs.md)
is the prompt: ranked candidates, each with the scenario, the code path, the visible cost, whether it is
reported, and a sketch of a reproduction in their own test suite. Treat its output as hypotheses: the next
step confirms them.

## 4. Adopt the harness in the fork

Put the contract where the project keeps tests of that kind, named the way its runner collects them
(Celery collects `test_*` classes, not `Test*`; Wagtail's runner ignores `*_test.py`, so its directory is
separate). Skip the directory on a Python older than the harness's, with a marker, not an error.

1. **Host.** Configure the harness for the project's stack (`django_host`, `redis_host`, or your own).
2. **Bindings are the project's real code.** A transition is the view, service or task the user's
   request reaches; recovery is what production runs (a worker pass, a periodic command, a restart). The
   harness refuses a binding that authors the behaviour in the test.
3. **A disposition for every profile**, claimed, declined or not applicable, each with a reason a
   maintainer would accept. Frameworks come with these already (`rq.worker_contract`,
   `django_tasks.worker_contract`, `celery_worker.worker_contract`).
4. **Histories.** One `HandoffHistory` (in process) or `ProcessHistory` (a real child process) per
   user-facing action, with an observation that includes what external systems saw: statuses, mails sent,
   webhooks delivered, money held.
5. **Run it and read the divergent histories.** Each label names the commit, call, reply, callback or
   receiver where the run went wrong, and what it left behind.

Add `--due-work-summary` to see every generated case with its outcome and each gap's reason. That output
is the capture the PR shows.

## 5. Confirm each finding independently

A harness run says *that* histories diverge. Before you write a sentence about it:

- reproduce the smallest version in a scratch script that uses the project's API, no harness, and print
  what it left behind;
- test the neighbouring claim you are tempted to make (the same failure without a retry configured, on
  another backend, on an older server) and state only what you saw. A "confirmed" result from an earlier
  pass is a hypothesis until you ran it;
- check the version dimension. Behaviour that differs by server version (Redis before 6.2 has no `BLMOVE`)
  or by library version belongs in the table for that version, and the finding says so;
- search the tracker again, with the words the maintainers use.

Drop a candidate that you cannot reproduce, or that the project documents and defends.

## 6. Declare, pin, and mark the magic

- **Findings.** Put a `Findings(delivered, {label: outcome})` on each history. The generated case holds the
  runs to that table and to the convergence verdict in one run; a declared gap is a strict xfail for the
  divergence alone, so a moved finding or a broken binding fails instead of passing as the known gap.
  Histories not listed must reach normal operation, so the table holds only the findings.
- **Gaps.** `handoff_gaps` (or `Claim(gaps=...)`) name each finding with its reason and a link to the
  issue. A new feature would forbid them; an existing project is `Adoption.LEGACY`.
- **The magic point.** The class under `@due_work_contract_suite(CONTRACT)` is empty on purpose, so a
  reviewer looking for the tests finds nothing. Put a comment above it that says so: the decorator reads
  the contract and generates the cases, none is written by hand, which generated case found which issue,
  and that a declared gap is a strict xfail that fails the run once fixed. The PR description links to it.
- **A plain test.** Beside their related tests, in their style, one `expectedFailure` (or `xfail`) per
  finding that fails for the stated reason. Remove the marker once and read the failure: it must be the
  assertion you meant, not a setup error, and the assertion after it must not be masked by the one before.

## 7. Improve the harness, then move the learning up

Ask what this probe needed that the harness did not have, and build it there:

- a fault the project can suffer that no history injects (the lost reply came from RQ);
- a way to bind a stack the harness cannot count commits in (Redis writes, a real worker process);
- something written twice in the fork, or in two integrations.

Then run a DRY round from the demo to the application to the integration to the core: if a helper written
for this project would serve the next one, move it up as far as it stays useful (a fault protocol into
core, a worker history into a shared module). Ship it as its own harness PR and release, so the upstream PRs
can pin `due-work-harness>=X`. See [CONTRIBUTING](../CONTRIBUTING.md) and [RELEASING](../RELEASING.md).

## 8. Disclose

Issues first, then one PR that references them. Templates are in
[`upstream-templates/`](upstream-templates/).

- **Every AI-authored comment opens with the self-identification line** in the form
  `> I'm {model}, AI can make mistakes. {a fresh three-to-five-word joke}`, and comments on incidents drop
  the joke. Follow the project's own template and policy instead of ours when they differ: Celery's issue
  template has a long checklist, and Wagtail asks for a human-written PR description and an "AI usage"
  section, which you fill in truthfully. If a policy says no, tell the person you work for and let them
  decide.
- **One issue per finding**, in plain words first: what the user sees, then the mechanism with file and
  line permalinks at a commit, then the smallest reproduction and its output, then how the harness found it.
  No fix, no suggestion of one.
- **The PR** has: a sentence that the change adds the failing tests and no fix; a *Where the magic
  happens* section with a permalink at the pushed commit to the decorated class, and the case list from
  `--due-work-summary`; the contract in a few bullets; the plain tests; and what was run and what it
  printed. Numbers in it are ones you have just measured.
- **A plain-language comment with ASCII art** on every issue and PR: two actors and a timeline for the
  bug, or the list of runs the harness makes for the PR. Concrete nouns, no jargon.
- **Say when CI is red for a reason that is not yours** (their `main` fails too), with the evidence, and
  do not change their test.

## 9. Ship and keep the PRs green

After the harness release, raise each fork's pin, run every contract against the installed package (not
the source), push, and watch CI. What went wrong the last time, so you can look for it first:

| Symptom | Cause |
| --- | --- |
| lockfile check fails | the lock needs the new release: with `uv`, raise the pin and the `exclude-newer-package` date, and change only the harness's entry (relocking everything rewrites hundreds of lines) |
| `ModuleNotFoundError` for the harness in one CI job | that job installs from its own environment (Hatch, tox), not the dev group: add the dependency there too |
| one matrix cell red | a server or library version behaves differently (Redis 5 vs 7): measure it there and pin a table per version |
| a bot commit on your branch (pre-commit.ci) | merge it in, never rebase or force-push; resolve to the project's import style |
| no checks at all | first-time contributors' workflows wait for a maintainer to approve them |
| red check that `main` also fails | baseline failure: prove it, say so on the PR |

Answer review comments: fix what is right, reply with what changed, and skip what is not.

## 10. Record it

Add the finding to the demo table and the README when the project is a demo the harness ships
([`demos/README.md`](../demos/README.md)), and keep the scratch material (captures, drafts) until the
threads close: maintainers' answers decide what to write next.

## Checklist

- [ ] target chosen, contribution policy and templates read, no duplicate report
- [ ] their own suite green on my machine; baseline known
- [ ] contract in the fork: real bindings, every profile disposed, histories observe what users see
- [ ] each finding reproduced by a script that uses their API, neighbours tested, versions checked
- [ ] `Findings` and gaps declared; the plain test fails for the stated reason
- [ ] magic-point comment above the decorated class
- [ ] harness gap closed upstream first, released, pins raised
- [ ] issues, PR, plain-language comments posted; self-ID on each; no fix offered
- [ ] CI watched to green, or red explained
