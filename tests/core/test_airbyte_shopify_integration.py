"""The Shopify adapter is importable without Airbyte or requests installed."""

import pytest

from due_work_harness.crash_histories import Findings
from due_work_harness.integrations.airbyte_shopify import MISSING_RESULT_URL, bulk_checkpoint_history


class NotAShopifyStream:
    pass


def test_bulk_history_requires_a_production_shopify_stream() -> None:
    with pytest.raises(AssertionError, match="source_shopify production stream"):
        bulk_checkpoint_history(
            stream_type=NotAShopifyStream,
            config={"start_date": "2023-01-01"},
            first_record={"id": "gid://shopify/Product/1"},
            later_record={"id": "gid://shopify/Product/2"},
        )


def test_bulk_history_rejects_nonserializable_test_data() -> None:
    stream_type = type("Products", (), {"__module__": "source_shopify.streams.streams"})
    with pytest.raises(TypeError, match="not JSON serializable"):
        bulk_checkpoint_history(
            stream_type=stream_type,
            config={"start_date": object()},
            first_record={"id": "gid://shopify/Product/1"},
            later_record={"id": "gid://shopify/Product/2"},
        )


def test_bulk_history_carries_the_adopters_pinned_finding() -> None:
    stream_type = type("Products", (), {"__module__": "source_shopify.streams.streams"})
    findings = Findings(delivered=((1, 2), "later"), outcomes={MISSING_RESULT_URL: ((2,), "later")})
    history = bulk_checkpoint_history(
        stream_type=stream_type,
        config={"start_date": "2023-01-01"},
        first_record={"id": "gid://shopify/Product/1"},
        later_record={"id": "gid://shopify/Product/2"},
        findings=findings,
    )

    assert history.failure_points == (MISSING_RESULT_URL,)
    assert history.death_points == ()
    assert history.findings == findings
