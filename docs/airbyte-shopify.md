# Airbyte Shopify bulk checkpoint integration

The [Shopify API fixtures](shopify.md) are usable by any Python app. This
Airbyte-specific binding, `due_work_harness.integrations.airbyte_shopify.bulk_checkpoint_history`, runs an
Airbyte `source_shopify` bulk stream against two synthetic Shopify result URLs.
It injects one external failure: a self-canceled bulk operation reports rows
but returns neither `url` nor `partialDataUrl`. The control supplies a Shopify
`partialDataUrl` for the canceled operation. The helper runs the real job
manager, record reader, state update and `stream_slices` retry calculation in
a child process. It returns a `ProcessHistory` for an adopter's
`DueWorkContract.process_handoffs`.

The adapter has no runtime dependency on Airbyte until the child executes.
The adopter must install its connector package and provide an
importable stream class. Pass synthetic, JSON-serializable config and records;
the adapter writes the case to a temporary file for the child. Never pass real
Shopify credentials.

```python
from due_work_harness.crash_histories import Findings
from due_work_harness.integrations.airbyte_shopify import (
    MISSING_RESULT_URL,
    bulk_checkpoint_history,
)
from source_shopify.streams.streams import Products

history = bulk_checkpoint_history(
    stream_type=Products,
    config={
        "shop": "test-shop",
        "start_date": "2023-01-01",
        "credentials": {"auth_method": "api_password", "api_password": "fake"},
        "authenticator": None,
    },
    first_record={
        "__typename": "Product",
        "id": "gid://shopify/Product/1",
        "updatedAt": "2023-01-01T12:00:00Z",
    },
    later_record={
        "__typename": "Product",
        "id": "gid://shopify/Product/2",
        "updatedAt": "2023-01-02T06:00:00Z",
    },
    findings=Findings(
        delivered=((1, 2), "2023-01-02T06:00:00+00:00"),
        outcomes={MISSING_RESULT_URL: ((2,), "2023-01-02T06:00:00+00:00")},
    ),
)
```

The adopter declares all six lifecycle and both safety dispositions in its
`DueWorkContract`, attaches `history`, and exposes a collected class with
`@due_work_contract_suite(CONTRACT)`. See [Airbyte PR #87592](https://github.com/airbytehq/airbyte/pull/87592)
for the full collected suite and the native regression. It reproduces the
existing [Airbyte issue #85372](https://github.com/airbytehq/airbyte/issues/85372)
with controlled responses; it does not make a live Shopify request.

This is a failure history, not a process kill. The child returns a nonzero
status *after* recording the missing-URL response so the harness verifies the
fault was reached. The connector itself continues silently. A declared legacy
gap becomes a strict XFAIL until the connector recovers the missing slice.
