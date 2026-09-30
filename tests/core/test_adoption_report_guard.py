"""Published verdict booleans cannot hide missing or failed execution evidence."""

import json
from pathlib import Path

import pytest

from scripts.check_adoption_report import check, read_report


@pytest.mark.parametrize("fault", [None, "missing", "deselected", "failed", "xfail"])
def test_adopter_report_checks_actual_evidence(tmp_path: Path, fault: str | None) -> None:
    path = tmp_path / "profiles.json"
    profile = {
        "title": "Indivisible Admission",
        "assessment": {"state": "claimed"},
        "cases": {
            "rollback": {
                "family": "admission",
                "collected": True,
                "selected": fault != "deselected",
                "outcomes": {
                    "setup": ["passed"],
                    "call": [fault if fault in ("failed", "xfail") else "passed"],
                    "teardown": ["passed"],
                },
                "passed": True,
            }
        },
        "verified": True,
    }
    path.write_text(
        json.dumps({} if fault == "missing" else {"test.Adopter": {"name": "adopter", "profiles": {"I": profile}}})
    )
    reports = read_report(path)
    if fault is None:
        check(reports, contracts=["adopter"], verified=["I"], exercised=["I"])
    else:
        with pytest.raises(AssertionError):
            check(reports, contracts=["adopter"], verified=["I"], exercised=[])
        if fault == "xfail":
            # A declared defect is executed evidence, never verification.
            check(reports, contracts=["adopter"], verified=[], exercised=["I"])
