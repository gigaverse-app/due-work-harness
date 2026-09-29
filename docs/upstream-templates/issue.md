> I'm {model}, AI can make mistakes. {joke}

### Summary

{What the user, organiser or money sees, in two or three plain sentences, before any mechanism. Name the
ordinary ways this happens: a deploy, an out-of-memory kill, a broker restart, a connection that drops.}

### Where

{The mechanism, with permalinks at a commit so the lines cannot move under the reader:}

- [`{function}`]({permalink to file#Lstart-Lend}) {what it commits and what it leaves for later};
- {the next step and what recovers it, or that nothing does}.

### Reproduction

{The smallest version, in the project's own test style, using its fixtures. Show the output, and the
failing assertion.}

```
{test}
```

```
{output}
```

What it costs, measured with the same setup:

- {a measured effect}
- {another, only if you ran it}

{Related issues, in one line each, and how they differ.}

### How this was found

This came out of [due-work-harness](https://github.com/gigaverse-app/due-work-harness), a pytest plugin
that checks background work is neither lost, repeated nor misrecorded. {Which integration or host it
used, which failures it injected, and which generated case reported this.} A PR follows with the test above
as an expected failure, and the harness contract that runs this case, and the others, against {project}.
