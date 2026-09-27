"""
What counts as a handoff site: a call that passes work to something that can lose it.

A *site* is a call in production code after which the work it names exists only
somewhere the process does not own: a callback that runs after the commit, a
message on a broker, a job row a worker will pick up, a workflow a runtime will
start. Each site needs a disposition — a contract that insures it, or a proven
exemption — and the coverage check finds every one.

Kinds are contributed per framework, and a kind is scanned by default when its
framework is importable (``[tool.due-work-harness] sites = [...]`` narrows it).
Matching is static and uses three shapes:

* **direct calls** by callee name: Django's ``on_commit``, Celery's
  ``send_task``, DBOS's ``start_workflow``. The names are specific enough to
  match on their own.
* **task methods**: ``.delay``/``.apply_async`` (Celery) or ``.defer`` (Procrastinate)
  called on a function the project registered as a task. The scan first
  indexes production functions decorated with the kind's task decorators
  (``@shared_task``, ``@app.task``), then resolves each receiver through the
  module's imports, so ``cache.delay(...)`` on an unrelated object is not a site.
* **task arguments**: a call such as DBOS's ``queue.enqueue(workflow, ...)``,
  whose first argument is a registered workflow.

Calls wrapped by ``sync_to_async(...)``/``async_to_sync(...)`` are unwrapped,
and ``.configure(...)``/``.s(...)``/``.si(...)``/``.signature(...)`` between a
task and its method are looked through.

Known limit: a handoff made in SQL (a stored procedure that enqueues) or through
a bound method passed along uncalled is invisible to a static scan.
"""

import importlib.util
from collections.abc import Collection, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class SiteKind:
    name: str
    #: The module whose presence enables this kind by default.
    requires: str
    #: Callee names that are sites wherever they are called.
    calls: frozenset[str] = frozenset()
    #: Decorator names (their last component) that register a deferred callable.
    task_decorators: frozenset[str] = frozenset()
    #: Methods that hand work off when called on a registered task.
    task_methods: frozenset[str] = frozenset()
    #: Calls that hand work off when their first argument is a registered task.
    task_argument_calls: frozenset[str] = frozenset()


DJANGO = SiteKind(name="django", requires="django", calls=frozenset({"on_commit"}))
CELERY = SiteKind(
    name="celery",
    requires="celery",
    calls=frozenset({"send_task"}),
    task_decorators=frozenset({"shared_task", "task"}),
    task_methods=frozenset({"delay", "apply_async"}),
)
PROCRASTINATE = SiteKind(
    name="procrastinate",
    requires="procrastinate",
    task_decorators=frozenset({"task"}),
    task_methods=frozenset({"defer", "defer_async"}),
)
DBOS = SiteKind(
    name="dbos",
    requires="dbos",
    calls=frozenset({"start_workflow", "start_workflow_async"}),
    task_decorators=frozenset({"workflow"}),
    task_argument_calls=frozenset({"enqueue", "enqueue_async"}),
)

BUILT_IN: dict[str, SiteKind] = {kind.name: kind for kind in (DJANGO, CELERY, PROCRASTINATE, DBOS)}

#: Adapters that run a callable later or elsewhere; ``wrapper(site)(...)`` is the site.
CALL_WRAPPERS = frozenset({"sync_to_async", "async_to_sync"})

#: Methods that return a configured copy of a task: ``task.configure(...).defer(...)``.
TASK_CONFIGURATORS = frozenset({"configure", "s", "si", "signature", "set"})


def installed_kinds() -> tuple[SiteKind, ...]:
    """The built-in kinds whose framework is importable, without importing it."""
    return tuple(kind for kind in BUILT_IN.values() if importlib.util.find_spec(kind.requires) is not None)


def resolve_kinds(names: Iterable[str] | None) -> tuple[SiteKind, ...]:
    """The kinds named in configuration, or every installed one when none are named."""
    if names is None:
        return installed_kinds()
    unknown = sorted(set(names) - set(BUILT_IN))
    if unknown:
        raise ValueError(f"unknown site kinds {unknown}; known kinds are {sorted(BUILT_IN)}")
    return tuple(BUILT_IN[name] for name in names)


def names(kinds: Collection[SiteKind]) -> list[str]:
    return [kind.name for kind in kinds]
