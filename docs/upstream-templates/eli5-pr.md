> I'm {model}, AI can make mistakes. {joke}

### ELI5: how this PR finds the bugs

The bugs are drawn in #{issue}. This comment is about how they were found: nobody guessed the scenario
first. The harness tries every break it knows, one at a time, against {the real code}.

```
  "{the action}" (one declaration: {arrange}, {run the real thing}, look)

   run 0    nothing breaks                        ──► {the reference outcome}
   run 1    {a death after step 1}                ─┐
   ...      ...                                    │  after each run, {recovery}
   run N    {a lost reply, a failing hook}        ─┘  then the result is compared with run 0

   same as run 0? ── yes ─► fine
                  └─ no ──► {a gap: the run and what it left}
```

Nobody wrote those runs. The file only says {which action to run and what to look at afterwards}; the list
of breaks comes from the harness, so it covers failures nobody thought to test.

Where it happens: the empty class decorated with `@due_work_contract_suite(CONTRACT)`, at
[lines {a}–{b}]({permalink}). It generates {N} cases; the {M} gaps show as strict XFAILs, so the suite stays
green today and each one fails the run the day it is fixed, until its declaration is removed. The PR
description lists every case.
