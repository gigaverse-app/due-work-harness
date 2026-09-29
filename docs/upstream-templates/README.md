# Templates for disclosing a finding

Copy these into a scratch directory and fill in every `{placeholder}`. They follow the
[upstream playbook](../upstream-playbook.md); where a project has its own issue or PR template or a policy
on AI-authored contributions, that wins, and these supply the content.

| File | Use |
| --- | --- |
| [`map-the-handoffs.md`](map-the-handoffs.md) | the prompt for an agent that maps a project's handoffs and ranks candidate weaknesses |
| [`issue.md`](issue.md) | one issue per finding |
| [`pr.md`](pr.md) | the PR that adds the failing tests and the contract |
| [`eli5-issue.md`](eli5-issue.md) | the plain-language comment with ASCII art on an issue |
| [`eli5-pr.md`](eli5-pr.md) | the same on the PR: how the harness found it |

Every comment starts with the self-identification line, in the form
`> I'm {model}, AI can make mistakes. {a fresh joke, three to five words}`. Drop the joke on incident,
security or postmortem threads.
