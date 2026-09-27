"""
Every handoff site in production has exactly one disposition.

The scan is static in both directions and imports nothing: it parses production
modules for handoff sites (see :mod:`.sites`), attributing each to the
outermost function or method that contains it, and parses test modules for the
declarations that account for them. It then requires the two to match exactly:

* **covered** — a generated contract suite names the function::

      @due_work_contract_suite(MY_CONTRACT, covers=(DueWorkSource(OrderService.place_order),))
      class TestOrdersDueWork: ...

* **exempt** — a generated exemption suite states why losing the handoff is
  acceptable and proves it with a harness probe or a production callable::

      @exempt_due_work_suite(
          DueWorkSource(cache.evict_after_commit),
          reason="the cache entry expires within a minute; the database stays authoritative",
          prove=LossIsAbsorbedElsewhere(strand=..., observe=..., absorb=...),
      )
      class TestCacheEvictionExemption: ...

* **baseline** — the site predates adoption and is listed in
  ``[tool.due-work-harness.baseline]``, which only shrinks.

A site with no disposition fails, and so does a disposition naming a function
with no site (stale), a function whose number of sites changed (drift), a
function with two dispositions, and a declaration that would not run as
written. A declaration counts only when pytest will collect and run it and it
is the harness's own: a module-level ``Test*`` class, not rebound later in its
module, carrying no skip or xfail mark, decorated by ``due_work_harness``'s
suite with ``covers`` as one inline tuple of ``due_work_harness``'s
``DueWorkSource(<imported production callable>)`` and a contract built in the
same module by ``due_work_harness``'s ``DueWorkContract`` (or
``ScheduledSelection``). ``pytest --due-work-verify`` closes the remaining gap
at run time: it fails unless every suite counted here ran.

Names are resolved through imports, relative imports, re-exports
(``from shop.orders import OrderService`` in ``shop/__init__.py``) and local
names, and matching is by exact qualified name, so two methods in one file can
never hide behind each other. Test modules are never imported, so the check
runs anywhere, fast, with no framework or database.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping

from due_work_harness.coverage.config import CoverageConfig
from due_work_harness.coverage.declarations import Declarations, declaration_modules
from due_work_harness.coverage.handoffs import production_sites, sites_by_function
from due_work_harness.coverage.project import Project
from due_work_harness.coverage.report import MODULE_LEVEL, CoverageReport, Disposition, Site
from due_work_harness.coverage.sites import kinds_for, names

__all__ = [
    "CoverageReport",
    "Disposition",
    "Site",
    "baseline_growth",
    "declaration_modules",
    "production_sites",
    "scan",
    "unaccounted_baseline",
]


def _unaccounted_message(qualified: str, sites: list[Site]) -> str:
    first = sites[0]
    kinds = ", ".join(sorted(Counter(site.kind for site in sites)))
    if qualified.endswith(f".{MODULE_LEVEL}"):
        return (
            f"{qualified} ({first.path}:{first.line}, {kinds}) hands work off outside any function, where no "
            f"DueWorkSource can name it: move it into the function that owns the handoff"
        )
    return (
        f"{qualified} ({first.path}:{first.line}, {kinds}) hands work off with no disposition: cover it with "
        f"DueWorkSource on the contract suite that insures it, or exempt it with proof"
    )


def scan(config: CoverageConfig) -> CoverageReport:
    """Find every site and every disposition, and every way they fail to match."""
    project = Project(config)
    kinds = kinds_for(project.frameworks, config.kinds)
    report = CoverageReport(kinds=names(kinds), sites=sites_by_function(project, kinds))
    report.problems.extend(
        f"{hidden} holds production code but `exclude` hides it from the scan: narrow the pattern"
        for hidden in project.hidden
    )
    declared: dict[str, list[Disposition]] = defaultdict(list)
    for module in declaration_modules(config):
        found, defects = Declarations(module, project).collect()
        report.problems.extend(defects)
        for qualified, disposition in found.items():
            declared[qualified].append(disposition)
    for configured, how, where in (
        (config.baseline, "baseline", "pyproject.toml [tool.due-work-harness.baseline]"),
        (config.bridges, "bridge", "pyproject.toml [tool.due-work-harness] bridges"),
    ):
        for qualified, count in configured.items():
            declared[project.canonical(qualified)].append(Disposition(how=how, sites=count, where=where))

    for qualified, dispositions in sorted(declared.items()):
        if len(dispositions) > 1:
            places = ", ".join(f"{d.how} in {d.where}" for d in dispositions)
            report.problems.append(f"{qualified} has more than one disposition: {places}")
        report.dispositions[qualified] = dispositions[0]

    for qualified in report.unaccounted:
        report.problems.append(_unaccounted_message(qualified, report.sites[qualified]))
    for qualified, disposition in sorted(report.dispositions.items()):
        actual = len(report.sites.get(qualified, []))
        if actual == 0:
            report.problems.append(
                f"{qualified} is {disposition.how} ({disposition.where}) but has no handoff site: remove the stale "
                f"disposition, or point it at the function that now hands the work off"
            )
        elif actual != disposition.sites:
            report.problems.append(
                f"{qualified} has {actual} handoff site(s) but its disposition ({disposition.how}, "
                f"{disposition.where}) accounts for {disposition.sites}: review it and update sites="
            )
    return report


def baseline_growth(current: Mapping[str, int], previous: Mapping[str, int]) -> list[str]:
    """Baseline entries added, or whose count grew, since ``previous``; the baseline only shrinks."""
    return sorted(name for name, count in current.items() if count > previous.get(name, 0))


def unaccounted_baseline(report: CoverageReport) -> dict[str, int]:
    """A baseline that would account for every currently unaccounted site, for first adoption."""
    return {qualified: len(report.sites[qualified]) for qualified in report.unaccounted}
