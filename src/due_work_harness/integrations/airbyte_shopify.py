"""Process histories for Airbyte's Shopify GraphQL bulk checkpoint boundary.

The adapter imports ``source_shopify`` only in a child process,
so the harness core stays usable without Airbyte installed. An adopter supplies
synthetic Shopify JSONL records and a connector stream class; the adapter runs
the real bulk manager, record reader, state update and retry slice selection.

This is a *failure* history, not a process-death history. The child exits with
status one after it records the missing-result response so ``ProcessHistory``
can verify that the injected fault actually occurred. The connector itself
continues normally when Shopify returns that response.
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from due_work_harness.crash_histories import Findings
from due_work_harness.integrations.shopify import (
    MISSING_RESULT_URL,
    BulkOperationStatus,
    ShopifyBulkDownload,
    ShopifyBulkExchange,
    ShopifyBulkOperation,
)
from due_work_harness.process_histories import ProcessHistory, fault_environment


def _status_response(operation: ShopifyBulkOperation) -> Any:
    return Mock(
        status_code=200,
        text="Shopify bulk status",
        **{"json.return_value": operation.graphql_response()},
    )


def _result_url(filename: str) -> str:
    return f"https://storage.googleapis.com/bulk?response-content-disposition=attachment%3B+filename%3D%22{filename}%22"


def _download_response(reply: ShopifyBulkDownload) -> Any:
    return Mock(status_code=reply.status_code, **{"iter_content.return_value": iter((reply.body,))})


def _stream(case: Mapping[str, Any]) -> Any:
    module = importlib.import_module(case["stream_module"])
    stream_type = getattr(module, case["stream_class"])
    return stream_type(case["config"])


def _consume_result(stream: Any, state: Mapping[str, Any], emitted: list[Any]) -> Mapping[str, Any]:
    manager = stream.job_manager
    if manager._job_result_filename is None:
        return state
    records = manager.record_producer.read_file(manager._job_result_filename)
    for record in stream.filter_records_newer_than_state(state, records):
        emitted.append(record["id"])
        state = stream.get_updated_state(state, record)
    return state


def _child(directory: Path) -> int:
    case = json.loads((directory / "case.json").read_text())
    previous = Path.cwd()
    os.chdir(directory)
    try:
        # ARRANGE: real Shopify bulk stream, with a small deterministic window.
        stream = _stream(case)
        manager = stream.job_manager
        manager._job_size = case["slice_days"]
        slices = iter(stream.stream_slices())
        first = next(slices)
        state = {stream.cursor_field: case["config"]["start_date"]}
        emitted: list[Any] = []
        first_exchange = ShopifyBulkExchange(
            operation=ShopifyBulkOperation(
                status=BulkOperationStatus.CANCELED,
                object_count=case["reported_count"],
                partial_data_url=_result_url("first.jsonl"),
            ),
            records=(case["first_record"],),
        )
        later_exchange = ShopifyBulkExchange(
            operation=ShopifyBulkOperation(
                status=BulkOperationStatus.COMPLETED,
                object_count=1,
                url=_result_url("later.jsonl"),
            ),
            records=(case["later_record"],),
        )

        def download(*, url: str, **_kwargs: Any) -> tuple[None, Any]:
            # EXTERNAL SEAM: only Shopify's result-storage HTTP response is supplied.
            exchange = first_exchange if "first.jsonl" in url else later_exchange
            return None, _download_response(exchange.download(url))

        manager.http_client.send_request = Mock(side_effect=download)
        manager._job_self_canceled = True
        manager._job_last_rec_count = case["reported_count"]
        canceled_operation = first_exchange.poll()
        missing_url = canceled_operation.partial_data_url is None

        # REAL PRODUCTION: checkpoint collection, record composition, stream
        # cursor tracking and the next slice all belong to source_shopify.
        manager._on_canceled_job(_status_response(canceled_operation))
        state = _consume_result(stream, state, emitted)
        second = next(slices)
        manager._on_completed_job(_status_response(later_exchange.poll()))
        state = _consume_result(stream, state, emitted)

        # OBSERVE: the later result can advance state even if the first result
        # was never available, excluding that first record from the retry.
        (directory / "result.json").write_text(
            json.dumps({"emitted": emitted, "state": state, "first": first, "second": second})
        )
        return int(missing_url)
    finally:
        os.chdir(previous)


def bulk_checkpoint_history(
    *,
    stream_type: type,
    config: Mapping[str, Any],
    first_record: Mapping[str, Any],
    later_record: Mapping[str, Any],
    findings: Findings | None = None,
    name: str = "self-canceled bulk job without partial result URL",
    reported_count: int = 4,
    slice_days: float = 1,
) -> ProcessHistory[Path, tuple[tuple[Any, ...], str | None]]:
    """Bind one Airbyte Shopify stream to the missing-partial-URL history.

    The stream type must be importable in a child process. ``config`` and both
    JSONL records must be JSON serializable test values, without live secrets.
    ``findings`` pins the expected control and fault observations in the
    adopter's ``DueWorkContract``. The adapter supplies the fault protocol and
    runs the production connector path; the adopter declares the verdict.
    """
    assert stream_type.__module__.startswith("source_shopify."), "bind an Airbyte source_shopify production stream"
    case = {
        "stream_module": stream_type.__module__,
        "stream_class": stream_type.__name__,
        "config": dict(config),
        "first_record": dict(first_record),
        "later_record": dict(later_record),
        "reported_count": reported_count,
        "slice_days": slice_days,
    }
    json.dumps(case)  # reject a non-serializable fixture before pytest collection finishes

    def run(point: str | None) -> tuple[Path, int]:
        directory = Path(tempfile.mkdtemp(prefix="airbyte-shopify-checkpoint-"))
        (directory / "case.json").write_text(json.dumps(case))
        child = subprocess.run(
            [sys.executable, "-m", "due_work_harness.integrations.airbyte_shopify", str(directory)],
            env={**os.environ, **fault_environment(point)},
            capture_output=True,
            text=True,
        )
        if child.returncode not in (0, 1) or not (directory / "result.json").exists():
            raise AssertionError(child.stderr)
        return directory, child.returncode

    def observe(directory: Path) -> tuple[tuple[Any, ...], str | None]:
        if not (directory / "result.json").exists():
            return (), None
        result = json.loads((directory / "result.json").read_text())
        return tuple(result["emitted"]), result.get("retry_start")

    def recover(directory: Path) -> None:
        # REAL PRODUCTION: Airbyte's stream_slices reads the last emitted state
        # to decide which Shopify window a retry requests.
        result = json.loads((directory / "result.json").read_text())
        stream = _stream(case)
        retry_slice = next(iter(stream.stream_slices(stream_state=result["state"])))
        result["retry_start"] = retry_slice["start"]
        (directory / "result.json").write_text(json.dumps(result))

    return ProcessHistory(
        name=name,
        initial=((), None),
        run=run,
        observe=observe,
        recover=recover,
        death_points=(),
        failure_points=(MISSING_RESULT_URL,),
        findings=findings,
    )


if __name__ == "__main__":
    sys.exit(_child(Path(sys.argv[1])))
