# Moving to pytest-obligation

New code uses the canonical package and contract:

```python
from pytest_obligation import ObligationContract, due_work_contract_suite
from pytest_obligation.integrations.django import django_host
```

Existing code continues to work without an import migration:

```python
from due_work_harness import DueWorkContract, due_work_contract_suite
from due_work_harness.integrations.django import django_host
```

`DueWorkContract` is an alias of `ObligationContract`, not a subclass or a second
model. `DueWorkContractDesignError` similarly aliases
`ObligationContractDesignError`. The contract fields, validation, decorators,
generated A–J tests, and framework bindings are unchanged.

## Compatibility boundary

All implementation lives in `src/pytest_obligation`. The old namespace lives
in `shims/due_work_harness`, with lazy module forwarding and typing stubs.
Old submodule imports resolve to the canonical module objects, sharing host
contexts, registries, class identity, and monkeypatches. Framework adapters
remain optional: importing either root package does not load them eagerly.
Historical pickle class paths resolve through the shim as well.

Static coverage scans accept both declaration namespaces and both contract
names without importing an application's tests. Existing pytest flags,
`due_work_harness_host`, `due_work` markers, `[tool.due-work-harness]` settings,
and the `due-work-harness` console command remain supported. The pytest entry
point retains its `due_work_harness` registration name, but loads
`pytest_obligation.pytest_plugin`; this avoids registering hooks twice and
preserves `-p due_work_harness` / `-p no:due_work_harness` behavior.

## Package-manager migration

Keep using the old distribution until the first renamed release is published.
For that release, replace the `due-work-harness` dependency with
`pytest-obligation`, retaining the same extras. Remove the old distribution
before installing the new one: both provide the old namespace and must not be
installed together. Keep the prior release's adoption guide if remaining on
an older version; the new import and contract names are not available there.
