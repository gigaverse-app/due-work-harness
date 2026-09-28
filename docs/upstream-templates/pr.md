> I'm {model}, AI can make mistakes. {joke}

Refs #{issue}, #{issue}

This adds the failing tests for #{issue} and the
[due-work-harness](https://github.com/gigaverse-app/due-work-harness) contract that found them, running
against {the project's code}. It adds no fix; the fixes are yours to shape.

### Where the magic happens

[`{path}` lines {a}–{b}]({permalink at the pushed commit}):

```python
@due_work_contract_suite(CONTRACT)
class {TestClass}:
    """Every case in this class is generated from CONTRACT; see the comment above."""
```

The class is empty on purpose. The decorator reads `CONTRACT` and generates every test case, bound to
{the real code it exercises}. None of the cases is written by hand. The file supplies {what the adopter
supplies}; the guarantees and their proofs come from the harness.

{Which generated case found which issue. Each is declared as a gap, so it is reported as a strict XFAIL: the
day it is fixed it passes, and the strict marker fails the run until the declaration is removed.}

What it generates, from `{command} --due-work-summary`:

```
{paste the summary}
```

### The contract: `{directory}`

- {profile or history} {what it binds, in one line}: {N} proofs, {M} pass, one is {issue}.
- {crash histories: what dies, what fails, what recovery is, and what the observation includes}.
- Each history's `findings` pin what every run leaves, in the same run as the verdict, so a change in the
  project moves an entry and the case names it.
- {the other profiles are declined or not applicable, each with the reason}.

{Python floor, dependency group or requirements file, and how the directory is skipped below the floor.}

### The plain tests: `{path}`

Beside {the related tests}, with no harness involved, {N} expected-failure tests:

- `{test name}` (#{issue}): `{the failing assertion}`.

### Tested

{Exactly what was run: environment, versions, commands, and the results: `{N} passed, {M} xfailed`.}
