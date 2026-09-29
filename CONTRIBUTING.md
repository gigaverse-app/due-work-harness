# Contributing

## Pull request titles

Pull requests are squash-merged with their title as the commit message, and the
title decides the next release, so it must be a
[Conventional Commit](https://www.conventionalcommits.org/): `fix: ...`,
`feat: ...`, `feat!: ...` for a breaking change, or `docs:`, `test:`, `ci:`,
`build:`, `refactor:`, `chore:` for changes that do not release on their own.
The `PR title` check enforces it. See [RELEASING.md](RELEASING.md).

## Tooling

The project is managed with [uv](https://docs.astral.sh/uv/). `uv.lock` is
committed, and CI installs from it with `--locked`, so a change to dependencies
must come with `uv lock`.

```bash
uv sync --all-extras                      # everything: the core, every integration, lint and type-check tools
uv run ruff check && uv run ruff format --check
uv run pyrefly check                      # the demos type-check against their upstream code: fetch it first (below)
```

## Tests

The core must work with no framework installed, and CI proves it with an
environment that has only the package and pytest:

```bash
uv sync --no-default-groups
uv run python scripts/check_core_is_framework_free.py
uv run pytest tests/core
```

The Django integration's self-tests need PostgreSQL (the standard `PG*`
variables; defaults `postgres:postgres@localhost:5432`):

```bash
uv sync --no-default-groups --extra django --extra celery
uv run pytest tests/django --ds=tests.django.settings
```

The demos run the harness against upstream demo applications pinned by commit:

```bash
uv run --no-project python demos/fetch_upstream.py
uv sync --no-default-groups --extra django --group demos
uv pip install -e demos/.upstream/procrastinate
PGDATABASE=procrastinate_demo uv run --no-sync pytest demos/procrastinate_demo_django --ds=demos.procrastinate_demo_django.settings
uv run --no-sync pytest demos/dbos_transactional_outbox -p no:django
```

## Rules the code follows

See [ARCHITECTURE.md](ARCHITECTURE.md). In short: nothing outside
`src/due_work_harness/integrations/` imports a framework; integrations import
theirs only when installed; every harness defense is self-tested in both
directions (a conforming binding passes, a counterfeit fails with its specific
message).

## Finding weaknesses in other projects

A new demo, an integration, or an upstream report follows [the upstream playbook](docs/upstream-playbook.md), with the templates in [`docs/upstream-templates`](docs/upstream-templates/). With Claude Code, the `find-upstream-weaknesses` skill in `.claude/skills` runs the whole cycle.
