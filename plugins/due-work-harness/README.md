# Due Work Harness plugin

This plugin teaches Codex and Claude Code to use [due-work-harness](https://github.com/gigaverse-app/due-work-harness) in a Python repository. It maps work the application owes, runs crash and retry histories against the real transition and recovery path, and reports what those histories prove. The plugin contains one skill and no MCP server. The pytest library is installed separately in the project under test.

Use it when a Celery task goes missing, a queue worker repeats a job, a Django transaction commits but the follow-up message is not published, or a Kafka consumer crashes between processing and committing its offset. It also helps test whether retries double-charge a customer, resend a receipt, or leave an order, notification, data import, or external API effect unfinished. These are examples of the failure modes to investigate, not claims that every integration is automatically covered. The skill names the boundaries the library actually instruments and their limits.

In Claude Code, add the repository as a marketplace, then install `due-work-harness@due-work-harness`. In Codex, install the package from the shared plugin directory once published, or load this plugin directory locally while developing it. Invoke `prove-due-work` explicitly or ask to crash-test a durable workflow.

The skill's instructions can be read in a chat, but executing proofs requires a coding environment with the target repository, Python, and the services used by its tests. See the [adoption guide](https://github.com/gigaverse-app/due-work-harness/blob/main/ADOPTING.md) for the package setup and contract requirements.
