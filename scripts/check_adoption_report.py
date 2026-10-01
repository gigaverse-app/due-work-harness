"""CI enrollment guard: a deleted or filtered adopter must not leave a green empty report."""

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import TypeAdapter

from pytest_obligation.profiles.coverage import SuiteCoverage


def read_report(path: Path) -> dict[str, SuiteCoverage]:
    """Validate canonical report models and recompute verdicts instead of trusting stored booleans."""
    data = json.loads(path.read_text())
    for suite in data.values():
        for profile in suite["profiles"].values():
            profile.pop("verified", None)
            for case in profile["cases"].values():
                case.pop("passed", None)
    return TypeAdapter(dict[str, SuiteCoverage]).validate_python(data)


def check(
    reports: Mapping[str, SuiteCoverage], *, contracts: list[str], verified: list[str], exercised: list[str]
) -> None:
    """Require named adopters and completed proofs, keeping known failures distinct from verification."""
    for name in contracts:
        matching = [suite for suite in reports.values() if suite.name == name]
        assert len(matching) == 1, f"expected exactly one collected contract named {name!r}, found {len(matching)}"
        suite = matching[0]
        for letter in verified:
            assert suite.profiles[letter].verified, f"{name}/{letter}: expected complete passing profile evidence"
        for letter in exercised:
            cases = suite.profiles[letter].cases
            assert cases, f"{name}/{letter}: expected behavioral proofs, not just an assessment"
            for identity, case in cases.items():
                assert case.collected and case.selected, f"{name}/{identity}: proof was not selected"
                assert (
                    case.outcomes.get("setup") == ["passed"]
                    and case.outcomes.get("call") in (["passed"], ["xfail"])
                    and case.outcomes.get("teardown") == ["passed"]
                ), f"{name}/{identity}: proof did not finish successfully or as its declared failure"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--contract", action="append", required=True)
    parser.add_argument("--verified", nargs="+", default=[])
    parser.add_argument("--exercised", nargs="+", default=[])
    arguments = parser.parse_args()
    check(
        read_report(arguments.report),
        contracts=arguments.contract,
        verified=arguments.verified,
        exercised=arguments.exercised,
    )
    print(f"Validated executed adoption for {len(arguments.contract)} contract(s)")


if __name__ == "__main__":
    main()
