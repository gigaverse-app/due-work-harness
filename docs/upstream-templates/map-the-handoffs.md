# Prompt: map a project's handoffs

Give this to an agent, with the placeholders filled. It reads and reproduces; it changes nothing in the
project. Its output is a set of hypotheses for the confirmation step of the playbook.

```text
You are analysing {project} ({one-line description}) at {path} (branch {branch}, commit {sha}, source
under {source dir}) to find concrete, reproducible weaknesses where work that a customer, an organiser or
money depends on is handed off AFTER a database commit and can be lost, done twice, or left half-written.
Do NOT write fixes. Read code (python and grep are allowed). Do not modify files under {path}.

Context: a static scan (`due-work-harness check`) found {N} functions that hand work off. The ones on the
user-facing path are: {list}. The project already recovers some of them: find which handoffs HAVE a
periodic recovery ({where the schedule lives, for example a beat schedule or a periodic-task signal}) and
which do NOT.

Map, with file:line references, the flow a user experiences: {step 1, for example an order is placed},
{step 2, it is paid}, {step 3, mails and documents are produced}, {step 4, integrations are notified}. For
each step list what is written in the same transaction, what follows the commit (`transaction.on_commit`,
`apply_async`, `.delay`, a signal), and what recovers it if the process dies right after the commit or the
broker refuses the publish.

Rank candidate weaknesses by (a) how visible the cost is to a user or organiser, (b) your confidence that
it can be reproduced with fault injection in the project's own tests (note how their test settings change
things, for example eager task execution or on_commit callbacks that never fire), (c) whether the project
already recovers it. For the top five give: the scenario in one sentence, the exact code path with line
numbers, the visible cost, whether it is already reported (`gh issue list --repo {owner}/{repo} --search
"<keywords>" --state all --limit 15`, citing numbers), and the smallest way to reproduce it in the
project's own test suite (which existing test and fixtures to build on).

Also report how the project's tests are run: config file, default database, required extras and system
packages, and any Python version constraint.

Keep the final report under 1500 words, dense and factual. Mark anything you did not run as code reading.
```
