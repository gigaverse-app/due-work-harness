"""Shopify fixtures preserve the API cases that matter to due-work histories."""

import json

import pytest
from pydantic import ValidationError

from due_work_harness.integrations.shopify import (
    EXPIRED_RESULT_URL,
    MISSING_RESULT_URL,
    BulkOperationStatus,
    ShopifyBulkExchange,
    ShopifyBulkOperation,
    shopify_jsonl,
)
from due_work_harness.process_histories import fault_environment


def test_canceled_operation_can_report_rows_without_a_result_url() -> None:
    response = ShopifyBulkOperation(status=BulkOperationStatus.CANCELED, object_count=4).graphql_response()

    assert response["data"]["node"] == {
        "id": "gid://shopify/BulkOperation/1",
        "status": "CANCELED",
        "objectCount": "4",
        "url": None,
        "partialDataUrl": None,
    }


def test_failed_operation_exposes_a_partial_result_url() -> None:
    operation = ShopifyBulkOperation(
        status=BulkOperationStatus.FAILED,
        object_count=3,
        root_object_count=2,
        partial_data_url="https://storage.example/partial.jsonl",
    )

    assert operation.node()["partialDataUrl"] == "https://storage.example/partial.jsonl"
    assert operation.node()["rootObjectCount"] == "2"
    assert operation.node()["url"] is None


def test_completed_operation_exposes_a_full_result_url() -> None:
    operation = ShopifyBulkOperation(
        status=BulkOperationStatus.COMPLETED,
        object_count=2,
        url="https://storage.example/complete.jsonl",
    )

    assert operation.node()["url"] == "https://storage.example/complete.jsonl"
    assert operation.node()["partialDataUrl"] is None


def test_negative_object_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ShopifyBulkOperation.model_validate({"status": "RUNNING", "object_count": -1})


def test_jsonl_preserves_parent_links_and_newlines_in_field_values() -> None:
    records = [
        {"id": "gid://shopify/Product/1", "title": "first\nsecond"},
        {"id": "gid://shopify/ProductVariant/2", "__parentId": "gid://shopify/Product/1"},
    ]

    encoded = shopify_jsonl(records)

    assert len(encoded.splitlines()) == 2
    assert [json.loads(line) for line in encoded.splitlines()] == records
    assert encoded.endswith(b"\n")


def test_missing_url_fault_changes_only_the_canceled_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    canceled = ShopifyBulkExchange(
        operation=ShopifyBulkOperation(
            status=BulkOperationStatus.CANCELED,
            object_count=4,
            partial_data_url="https://storage.example/partial.jsonl",
        )
    )
    completed = ShopifyBulkExchange(
        operation=ShopifyBulkOperation(
            status=BulkOperationStatus.COMPLETED,
            object_count=1,
            url="https://storage.example/complete.jsonl",
        )
    )
    assert canceled.poll().partial_data_url == "https://storage.example/partial.jsonl"

    for name, value in fault_environment(MISSING_RESULT_URL).items():
        monkeypatch.setenv(name, value)

    assert canceled.poll().partial_data_url is None
    assert canceled.operation.partial_data_url == "https://storage.example/partial.jsonl"
    assert completed.poll().url == "https://storage.example/complete.jsonl"


def test_expired_signed_url_returns_an_http_failure_without_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://storage.example/partial.jsonl"
    exchange = ShopifyBulkExchange(
        operation=ShopifyBulkOperation(status=BulkOperationStatus.FAILED, object_count=1, partial_data_url=url),
        records=({"id": "gid://shopify/Product/1"},),
    )
    assert exchange.download(url).status_code == 200
    assert json.loads(exchange.download(url).body) == {"id": "gid://shopify/Product/1"}

    for name, value in fault_environment(EXPIRED_RESULT_URL).items():
        monkeypatch.setenv(name, value)

    expired = exchange.download(url)
    assert expired.status_code == 403
    assert expired.body == b""


def test_download_rejects_a_url_from_another_operation() -> None:
    exchange = ShopifyBulkExchange(
        operation=ShopifyBulkOperation(
            status=BulkOperationStatus.COMPLETED,
            object_count=1,
            url="https://storage.example/complete.jsonl",
        )
    )

    with pytest.raises(ValueError, match="does not belong"):
        exchange.download("https://storage.example/other.jsonl")
