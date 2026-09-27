"""
Render executed observation evidence for comparison; never infer test equivalence.

python -m due_work_harness.evidence.observation_report report.json --field event_id
"""

import argparse
import json
from pathlib import Path

from due_work_harness.evidence.observation import ObservationReport, observed_test_from_json


class _ReportAdapter:
    """The report's one JSON boundary: written by workers and the report file, read by the CLI."""

    def dump_json(self, tests: ObservationReport, *, indent: int | None = None, context: Path | None = None) -> bytes:
        document = {nodeid: [test.to_json(context) for test in executions] for nodeid, executions in tests.items()}
        return json.dumps(document, indent=indent, ensure_ascii=False).encode("utf-8")

    def validate_json(self, data: bytes | str) -> ObservationReport:
        document = json.loads(data)
        assert isinstance(document, dict), "an observation report must be a JSON object keyed by test node id"
        report: ObservationReport = {}
        for nodeid, executions in document.items():
            assert isinstance(executions, list), f"{nodeid}: executions must be a JSON array"
            report[nodeid] = [observed_test_from_json(execution) for execution in executions]
        return report


REPORT_ADAPTER = _ReportAdapter()


def report_json(tests: ObservationReport, root: Path) -> bytes:
    """One canonical serialization boundary for worker transport and the report file."""

    return REPORT_ADAPTER.dump_json(tests, indent=2, context=root) + b"\n"


def comparison_markdown(tests: ObservationReport, *, field: str | None = None) -> str:
    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    rows = [
        "Executed equality checks only. Matching field names do not establish equivalent histories or boundaries.",
        "Unlisted assertions and unselected tests remain unknown; use the effect report to audit expected cases.",
        "",
        "| Test | Kind | Test outcome | Execution | Observation | Fields | Equality | Assertion |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for nodeid, executions in sorted(tests.items()):
        for index, test in enumerate(executions, start=1):
            for check in test.checks:
                if field is not None and field not in check.fields:
                    continue
                rows.append(
                    "| "
                    + " | ".join(
                        map(
                            cell,
                            (
                                nodeid,
                                test.kind,
                                test.status,
                                str(index),
                                check.because,
                                ", ".join(check.fields),
                                "matched" if check.matched else "mismatched",
                                f"{check.path}:{check.line}",
                            ),
                        )
                    )
                    + " |"
                )
    if len(rows) == 5:
        rows.append("\nNo executed checks match. This is not evidence that a guarantee is absent or covered.")
    return "\n".join(rows) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--field", help="Show only checks with this exact observation field name")
    arguments = parser.parse_args()
    # Reuse the worker/file schema at the CLI input boundary.
    tests = REPORT_ADAPTER.validate_json(arguments.report.read_bytes())
    print(comparison_markdown(tests, field=arguments.field), end="")


if __name__ == "__main__":
    main()
