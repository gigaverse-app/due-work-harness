"""Transport-neutral Shopify GraphQL bulk-operation fixtures for due-work proofs.

These values describe Shopify's external replies. Adopters bind them at their
own HTTP boundary and let their production code decide what to retry, emit or
checkpoint. In particular, a nonzero ``objectCount`` does not promise a result
URL: Shopify can return ``null`` for both ``url`` and ``partialDataUrl``.
"""

import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import Field

from due_work_harness.models import HarnessModel
from due_work_harness.process_histories import fault_fires

MISSING_RESULT_URL = "canceled without partial result URL"
EXPIRED_RESULT_URL = "result URL expired"


class BulkOperationStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    CANCELING = "CANCELING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class ShopifyBulkOperation(HarnessModel):
    """A GraphQL Admin API bulk-operation snapshot supplied at the HTTP seam."""

    id: str = "gid://shopify/BulkOperation/1"
    status: BulkOperationStatus
    object_count: int = Field(ge=0)
    root_object_count: int | None = Field(default=None, ge=0)
    url: str | None = None
    partial_data_url: str | None = None

    def node(self) -> dict[str, Any]:
        """Fields returned by a ``node(id:)`` status poll."""
        node = {
            "id": self.id,
            "status": self.status.value,
            "objectCount": str(self.object_count),
            "url": self.url,
            "partialDataUrl": self.partial_data_url,
        }
        if self.root_object_count is not None:
            node["rootObjectCount"] = str(self.root_object_count)
        return node

    def graphql_response(self) -> dict[str, Any]:
        """A Shopify GraphQL response body for a bulk-operation status poll."""
        return {"data": {"node": self.node()}}


def shopify_jsonl(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Encode bulk result rows, preserving Shopify's one-object-per-line wire format."""
    return b"".join((json.dumps(dict(record), separators=(",", ":")) + "\n").encode() for record in records)


class ShopifyBulkDownload(HarnessModel):
    """A transport-neutral signed-URL download reply for the adopter to serve."""

    status_code: int = Field(ge=100, le=599)
    body: bytes


class ShopifyBulkExchange(HarnessModel):
    """Shopify replies with injectable missing and expired bulk result URLs.

    Bind ``poll`` and ``download`` to the adopter's HTTP client in a child
    process. The application owns the retry and checkpoint behavior; this
    exchange owns only the external replies at the Shopify boundary.
    """

    operation: ShopifyBulkOperation
    records: tuple[dict[str, Any], ...] = ()

    def poll(self) -> ShopifyBulkOperation:
        if self.operation.status == BulkOperationStatus.CANCELED and fault_fires(MISSING_RESULT_URL):
            return self.operation.model_copy(update={"url": None, "partial_data_url": None})
        return self.operation

    def download(self, url: str) -> ShopifyBulkDownload:
        if url not in (self.operation.url, self.operation.partial_data_url) or not url:
            raise ValueError("download URL does not belong to this Shopify bulk operation")
        if fault_fires(EXPIRED_RESULT_URL):
            return ShopifyBulkDownload(status_code=403, body=b"")
        return ShopifyBulkDownload(status_code=200, body=shopify_jsonl(self.records))
