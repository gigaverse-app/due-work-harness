"""
What counts as a handoff site: a reference to something that passes work to where it can be lost.

A *site* is a place in production code after which the work it names exists
only somewhere the process does not own: a callback that runs after the commit,
a message on a broker, a job row a worker will pick up, a workflow a runtime
will start. Each site needs a disposition — a contract that insures it, or a
proven exemption — and the coverage check finds every one.

Kinds are contributed per framework. A kind is scanned whenever the project's
production code imports its framework, directly or through another module of
the project; ``[tool.due-work-harness] sites = [...]`` adds kinds the scan
cannot detect (a framework reached only through a third-party wrapper) and
can never remove a detected one. Detection is static, so the check finds the
same sites whether or not the framework is installed where it runs.

A site is a *reference*, called or not, so a handoff passed along is still
found: ``sync_to_async(on_commit)(...)``, ``partial(on_commit, callback)`` and
``callbacks.append(transaction.on_commit)`` are each one site. Binding a
reference to a name (``hook = transaction.on_commit``, ``self.hook = ...``, a
parameter default) is not itself a site; each use of that name is. Three shapes
match:

* **direct callables** by name: Django's ``on_commit``, Celery's ``send_task``,
  DBOS's ``start_workflow``. The names are specific enough to match on their
  own, including under an import alias.
* **task methods**: ``.delay``/``.apply_async`` (Celery) or ``.defer``
  (Procrastinate) on a function the project registers as a task, by decorator
  (``@shared_task``, ``@app.task``) or by assignment (``send = shared_task(_send)``).
  The receiver is resolved through imports, re-exports and local names, so
  ``cache.delay(...)`` on an unrelated object is not a site, and
  ``.configure(...)``/``.s(...)``/``.si(...)``/``.signature(...)``/``.set(...)``
  between a task and its method are looked through.
* **task arguments**: a call such as DBOS's ``queue.enqueue(workflow, ...)``,
  whose first argument is a registered workflow.

Known limits: a handoff made in SQL (a stored procedure that enqueues), reached
through ``getattr`` with a computed name, or made on a task class instance
(Celery's class-based tasks) is invisible to a static scan.
"""

from collections.abc import Collection, Iterable

from due_work_harness.models import HarnessModel


class SiteKind(HarnessModel):
    name: str
    #: The top-level module whose import enables this kind.
    requires: str
    #: Callable names that are sites wherever they are referenced.
    calls: frozenset[str] = frozenset()
    #: Decorator names (their last component) that register a deferred callable.
    task_decorators: frozenset[str] = frozenset()
    #: Methods that hand work off when referenced on a registered task.
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

#: Methods that return a configured copy of a task: ``task.configure(...).defer(...)``.
TASK_CONFIGURATORS = frozenset({"configure", "s", "si", "signature", "set"})


def resolve_kinds(names: Iterable[str]) -> tuple[SiteKind, ...]:
    """The built-in kinds with these names; an unknown name is a configuration error."""
    names = tuple(names)
    unknown = sorted(set(names) - set(BUILT_IN))
    if unknown:
        raise ValueError(f"unknown site kinds {unknown}; known kinds are {sorted(BUILT_IN)}")
    return tuple(BUILT_IN[name] for name in names)


def kinds_for(imported: Collection[str], configured: Collection[SiteKind] = ()) -> tuple[SiteKind, ...]:
    """The kinds whose framework is among ``imported`` top-level modules, plus ``configured``, in built-in order."""
    wanted = {kind.name for kind in configured} | {kind.name for kind in BUILT_IN.values() if kind.requires in imported}
    return tuple(kind for kind in BUILT_IN.values() if kind.name in wanted)


def names(kinds: Collection[SiteKind]) -> list[str]:
    return [kind.name for kind in kinds]
