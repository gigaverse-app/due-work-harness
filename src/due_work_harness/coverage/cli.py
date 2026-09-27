"""
``due-work-harness``: the coverage check from the command line and CI.

    due-work-harness check [--root DIR] [--base-ref REF]
    due-work-harness sites [--root DIR]
    due-work-harness baseline [--root DIR]

``check`` exits 1 on any problem. With ``--base-ref`` it also refuses baseline
entries added, or whose count grew, since that git ref: the baseline records
sites that predate adoption, and it only shrinks. ``sites`` prints the inventory with each site's
disposition. ``baseline`` prints a ``[tool.due-work-harness.baseline]`` table for
the currently unaccounted sites, the starting point when adopting the harness in
an existing project.
"""

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from due_work_harness.coverage.config import baseline_from, load_config
from due_work_harness.coverage.scan import baseline_growth, scan, unaccounted_baseline


class MissingBaseRef(ValueError):
    """The base ref is not in the local repository; a shallow checkout must fetch it."""


def _previous_baseline(root: Path, ref: str) -> dict[str, int] | None:
    """The baseline at ``ref``, or ``None`` when the project had not adopted the harness there."""
    known = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if known.returncode != 0:
        raise MissingBaseRef(
            f"base ref {ref!r} is not in this repository, so the baseline cannot be compared; fetch it first "
            f"(for example `git fetch --depth=1 origin {ref}`)"
        )
    shown = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:./pyproject.toml"], capture_output=True, text=True, check=False
    )
    return baseline_from(shown.stdout) if shown.returncode == 0 else None


def _check(root: Path, base_ref: str | None) -> int:
    config = load_config(root)
    report = scan(config)
    problems = list(report.problems)
    if base_ref:
        previous = _previous_baseline(config.root, base_ref)
        if previous is not None:
            problems.extend(
                f"{name} grew in the baseline since {base_ref} ({previous.get(name, 0)} -> {config.baseline[name]} "
                f"site(s)); the baseline only shrinks, so cover or exempt it"
                for name in baseline_growth(config.baseline, previous)
            )
    sites = sum(len(found) for found in report.sites.values())
    kinds = ", ".join(report.kinds) or "no framework detected"
    if problems:
        print(f"due-work-harness: {len(problems)} problem(s) across {sites} handoff site(s) [{kinds}]")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        f"due-work-harness: every one of {sites} handoff site(s) in {len(report.sites)} function(s) is accounted for [{kinds}]"
    )
    return 0


def _sites(root: Path) -> int:
    report = scan(load_config(root))
    for qualified in sorted(report.sites):
        disposition = report.dispositions.get(qualified)
        status = f"{disposition.how}: {disposition.where}" if disposition else "UNACCOUNTED"
        for site in report.sites[qualified]:
            print(f"{site.path}:{site.line}\t{site.kind}\t{qualified}\t{status}")
    return 0


def _baseline(root: Path) -> int:
    entries = unaccounted_baseline(scan(load_config(root)))
    print("[tool.due-work-harness.baseline]")
    for qualified, count in sorted(entries.items()):
        print(f'"{qualified}" = {count}')
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="due-work-harness", description=__doc__.split("\n\n")[0].strip())
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("check", "fail unless every handoff site has exactly one disposition"),
        ("sites", "list every handoff site and its disposition"),
        ("baseline", "print a baseline table for the currently unaccounted sites"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--root", type=Path, default=Path("."), help="the project directory (default: .)")
        if name == "check":
            command.add_argument("--base-ref", help="git ref whose baseline this one may only shrink from")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "check":
            return _check(arguments.root, arguments.base_ref)
        if arguments.command == "sites":
            return _sites(arguments.root)
        return _baseline(arguments.root)
    except ValueError as error:
        print(f"due-work-harness: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
