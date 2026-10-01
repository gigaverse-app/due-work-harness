# Due Work Harness as a pytest plugin

Due Work Harness generates crash, retry, and recovery tests from a
`DueWorkContract` bound to an application's production code. The generated
cases run alongside ordinary pytest tests, using the project's fixtures and
test services. The Claude/Codex skills help an agent create those bindings;
the Python package supplies the pytest integration and fault-injection engine.

## Install and discover

Install in the Python environment that runs your application's tests:

```bash
pip install due-work-harness
# Or choose your project's package manager:
uv add --dev due-work-harness
poetry add --group dev due-work-harness
```

Pytest automatically loads installed plugins registered in the `pytest11`
entry-point group. This distribution registers:

```toml
[project.entry-points.pytest11]
due_work_harness = "due_work_harness.pytest_plugin"
```

Confirm the registration and available options:

```bash
pytest --trace-config --collect-only
pytest --help
```

If your environment disables plugin autoload, load it explicitly with
`pytest -p due_work_harness`. To disable it, use `pytest -p no:due_work_harness`.
See [pytest's plugin-discovery documentation](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others).

The PyPI metadata includes `Framework :: Pytest`, a pytest-focused summary and
keywords, and documentation/source/changelog links. The classifier helps people
and catalogs identify it as a pytest plugin; the entry point makes pytest load
it. Installing the plugin alone does not generate application tests: first
declare a contract and expose a class decorated with
`@due_work_contract_suite(CONTRACT)`. Follow the [adoption guide](../ADOPTING.md)
and [runnable example](../examples/adopter/README.md).

## Generated tests and selection

The contract decorator generates pytest cases at collection time, rather than
writing test source files. Applicable A–J profiles exercise lost messages,
worker deaths, uncertain external effects, competing results, replay, atomic
admission, and retry limits. New checks for an already-bound profile can extend
an existing suite after a package upgrade; new profiles or bindings require
assessment. See [the guarantee catalog](how-it-works.md#the-a-j-guarantees).

Generated cases carry the registered `due_work` marker. Optional generated
schedule searches also carry `due_work_exploration`:

```bash
pytest --collect-only -m due_work
pytest -m due_work --due-work-summary
```

Normal pytest selection, fixtures, JUnit reports, and CI runners still apply.
Summary and executed-profile reporting support pytest-xdist; findings recording
requires a single process.

## Options and reports

| Option | Behavior |
| --- | --- |
| `--due-work-summary` | Lists each generated suite's cases, outcomes, and known-gap reasons. |
| `--due-work-profile-report=profiles.json` | Writes declared, collected, selected, and executed A–J evidence, including setup/call/teardown results. |
| `--due-work-require-assessed` | Rejects unassessed profiles and convergence families before pytest filters cases. |
| `--due-work-verify` | Fails if a contract or exemption counted by the static handoff scan ran no case in this session. Requires scan configuration. |
| `--due-work-record-findings` | Prints observed history findings for review before pinning them. Run without xdist's `-n`; this mode records instead of checking declared findings tables. |
| `--due-work-explore=smoke` or `deep` | Enables additional Hypothesis schedule search; requires the `[exploration]` extra. |

Use `--junitxml=results.xml` for pytest's standard CI artifact. Known gaps are
strict XFAILs; `NotAssessed` records unfinished assessment. Neither is a verified
guarantee. The [green-result guide](what-a-green-result-means.md) explains the
scope of a pass and how to assess findings.

## Host configuration

Configure the host appropriate to the real database or execution boundary in
`conftest.py`, as described in [integrations](integrations.md). Alternatively,
the plugin accepts a dotted path to a `Host` or a zero-argument host factory:

```toml
[tool.pytest.ini_options]
due_work_harness_host = "myapp.testing:make_due_work_host"
```

The host is configured before collection. The application still supplies
production transitions, independent observations, and real recovery behavior.
The [handoff coverage scan](coverage.md) complements runtime proofs by finding
production handoffs that have not yet been accounted for.
