"""Optional pytest view of A–J coverage, for both native and registered work-table suites."""

from collections.abc import Generator
from typing import Any

import pytest

from .coverage import Assessment, CaseEvidence, ProfileCoverage, SuiteCoverage

_REPORT = pytest.StashKey[dict[str, SuiteCoverage]]()
_ITEMS = pytest.StashKey[dict[str, tuple[str, str]]]()


def _record(config: pytest.Config, suite: str, case_id: str, *, selected: bool = False) -> None:
    for profile in config.stash[_REPORT][suite].profiles.values():
        if case_id in profile.cases:
            evidence = profile.cases[case_id]
            evidence.collected = True
            evidence.selected |= selected


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Generator[None]:
    """Capture expected/collected coverage before filtering, then record actual selection."""
    if not config.getoption("due_work_profile_report", default=None):
        yield
        return
    # These declarations have already been imported by selected suites. Keeping
    # imports here avoids adding domain models to the global plugin import path.
    from ..contract import (
        Claim,
        ContractCase,
        Disposition,
        ObligationContract,
        SafetyContract,
        convergence_assessments,
        disposition_label,
        safety_contract_cases,
        suite_cases,
    )
    from .catalog import Profile

    reports: dict[str, SuiteCoverage] = {}
    names: dict[str, tuple[str, str]] = {}
    for item in items:
        if not isinstance(item, pytest.Function) or item.cls is None:
            continue
        contract = getattr(item.cls, "__due_work_contract__", None) or getattr(item.cls, "__safety_contract__", None)
        if not isinstance(contract, (ObligationContract, SafetyContract)):
            continue
        suite = f"{item.cls.__module__}.{item.cls.__qualname__}"
        if suite not in reports:

            def assessment(disposition: Disposition) -> Assessment:
                return Assessment(
                    state=disposition_label(disposition),
                    because=None if isinstance(disposition, Claim) else disposition.because,
                )

            profiles = {
                profile.name: ProfileCoverage(title=profile.title, assessment=assessment(disposition))
                for profile, disposition in contract.profiles.items()
            }
            if isinstance(contract, ObligationContract):
                profiles[Profile.E.name].families = {
                    family.name: assessment(disposition)
                    for family, disposition in convergence_assessments(contract).items()
                }
                parameters = suite_cases(contract, covers=getattr(item.cls, "__due_work_sources__", ()))
            else:
                parameters = safety_contract_cases(contract)
            for parameter in parameters:
                case = parameter.values[0]
                if case.profile is None or case.assessment_only:
                    continue
                for profile in (case.profile, *case.related_profiles):
                    profiles[profile.name].cases[case.id] = CaseEvidence(family=case.family)
            reports[suite] = SuiteCoverage(name=contract.name, profiles=profiles)
        if hasattr(item, "callspec") and "case" in item.callspec.params:
            case = item.callspec.params["case"]
            assert isinstance(case, ContractCase), "generated profile test must carry the canonical case"
            names[item.nodeid] = (suite, case.id)
    config.stash[_REPORT] = reports
    config.stash[_ITEMS] = names
    for suite, case_id in names.values():
        _record(config, suite, case_id)
    yield
    for item in items:
        if item.nodeid in names:
            _record(config, *names[item.nodeid], selected=True)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Read the final normalized outcome, including teardown and strict known failures."""
    report = yield
    route = item.config.stash.get(_ITEMS, {}).get(item.nodeid)
    if route is None:
        return report
    suite, case_id = route
    outcome = report.outcome
    if getattr(report, "wasxfail", None) is not None:
        outcome = "xpass" if report.passed else "xfail"
    for profile in item.config.stash[_REPORT][suite].profiles.values():
        if case_id in profile.cases:
            profile.cases[case_id].outcomes.setdefault(report.when, []).append(outcome)
    return report


def merge_reports(target: dict[str, SuiteCoverage], incoming: dict[str, SuiteCoverage]) -> None:
    """Merge distributed evidence only when workers agree on the same declaration and expected cases."""
    for suite, report in incoming.items():
        if suite not in target:
            target[suite] = report
            continue
        current = target[suite]
        assert current.name == report.name and current.profiles.keys() == report.profiles.keys()
        for letter, profile in report.profiles.items():
            existing = current.profiles[letter]
            assert (existing.title, existing.assessment, existing.families, existing.cases.keys()) == (
                profile.title,
                profile.assessment,
                profile.families,
                profile.cases.keys(),
            ), f"{suite}/{letter}: workers disagree on expected profile coverage"
            for case_id, case in profile.cases.items():
                old = existing.cases[case_id]
                assert old.family == case.family
                old.collected |= case.collected
                old.selected |= case.selected
                for phase, outcomes in case.outcomes.items():
                    old.outcomes.setdefault(phase, []).extend(outcomes)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    """The dynamic xdist boundary returns metadata only, with no application values."""
    incoming = {
        name: SuiteCoverage.model_validate(value)
        for name, value in node.workeroutput.get("due_work_profiles", {}).items()
    }
    merge_reports(node.config.stash.setdefault(_REPORT, {}), incoming)


def pytest_sessionfinish(session: pytest.Session) -> None:
    destination = session.config.getoption("due_work_profile_report", default=None)
    if not destination:
        return
    from pathlib import Path

    from pydantic import TypeAdapter

    reports = session.config.stash.get(_REPORT, {})
    # Computed verdicts are regenerated after xdist merging, never trusted as input.
    serialized = {name: report.model_dump(round_trip=True) for name, report in reports.items()}
    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        worker_output["due_work_profiles"] = serialized
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TypeAdapter(dict[str, SuiteCoverage]).dump_json(reports, indent=2))
