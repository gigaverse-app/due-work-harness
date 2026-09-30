# Shopify GraphQL bulk API fixtures

`due_work_harness.integrations.shopify` supplies validated, transport-neutral
Shopify bulk-operation snapshots and JSONL result bytes. Use them at your app's
GraphQL and signed-result-URL HTTP boundaries, then put the app's own transition,
recovery and observation in a `ProcessHistory` or `HandoffHistory`. The module
does not import a Shopify SDK or make network requests.

```python
from due_work_harness.integrations.shopify import (
    EXPIRED_RESULT_URL,
    MISSING_RESULT_URL,
    BulkOperationStatus,
    ShopifyBulkExchange,
    ShopifyBulkOperation,
    shopify_jsonl,
)

status_body = ShopifyBulkOperation(
    status=BulkOperationStatus.CANCELED,
    object_count=4,
    partial_data_url=None,
).graphql_response()
# {"data": {"node": {"status": "CANCELED", "objectCount": "4", ...}}}

result_body = shopify_jsonl([
    {"id": "gid://shopify/Product/1", "title": "one"},
    {"id": "gid://shopify/ProductVariant/2", "__parentId": "gid://shopify/Product/1"},
])

exchange = ShopifyBulkExchange(
    operation=ShopifyBulkOperation(
        status=BulkOperationStatus.CANCELED,
        object_count=4,
        partial_data_url="https://storage.example/partial.jsonl",
    ),
    records=({"id": "gid://shopify/Product/1"},),
)
# Bind exchange.poll().graphql_response() to the app's status HTTP seam.
# Bind exchange.download(url) to its signed-URL download seam.
```

Under `fault_environment(MISSING_RESULT_URL)`, `exchange.poll()` removes the
canceled operation's result URLs. Under `fault_environment(EXPIRED_RESULT_URL)`,
`exchange.download(url)` returns a synthetic HTTP 403 with an empty body.
Adopters choose which named points to place in `ProcessHistory.failure_points`
and map these transport-neutral replies to their own HTTP client. A 403 is the
fixture's expired-link failure, not a claim that Shopify always returns that
specific status for an expired link. Keep the observation focused on what the
application emitted and where its *production retry* resumes. A nonzero
`objectCount` counts processed objects, including nested objects; it does not
guarantee a downloadable result. The fixture deliberately permits a positive
count with both URLs absent.

Shopify documents [bulk operation fields and the seven-day result URL lifetime](https://shopify.dev/docs/api/admin-graphql/2026-04/objects/BulkOperation)
and the [bulk query workflow](https://shopify.dev/docs/apps/build/apis/graphql-admin/bulk-operations/queries).
The harness fixtures model those wire cases; they make no claim that a live
Shopify store was queried.

For a concrete adopter, [the Airbyte Shopify adapter](airbyte-shopify.md) binds
these fixtures to Airbyte's production `source_shopify` stream and exposes a
`ProcessHistory`. Airbyte's collected contract lives in
[PR #87592](https://github.com/airbytehq/airbyte/pull/87592).
