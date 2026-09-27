"""Reporting preserves the assertion and records no application values."""

# A dataclass on purpose: observations accept the value types adopters already have.
from dataclasses import dataclass, field

import pytest

from due_work_harness.evidence.observation import assert_observation, record_observations


@dataclass(frozen=True)
class ProductObservation:
    event_id: str
    status: str
    diagnostic: str = field(default="", compare=False)


def test_named_observation_records_its_assertion_without_values() -> None:
    with record_observations() as checks:
        assert_observation(
            ProductObservation("private-event", "ready", diagnostic="ignored-one"),
            ProductObservation("private-event", "ready", diagnostic="ignored-two"),
            because="correct publication",
        )
    assert checks[0].fields == ["event_id", "status"]
    assert checks[0].matched
    assert "private-event" not in repr(checks)
    assert checks[0].path == __file__


def test_nested_recording_and_failed_equality_do_not_leak_between_tests() -> None:
    with record_observations() as outer:
        with record_observations() as inner, pytest.raises(AssertionError, match="changed"):
            assert_observation({"result": "private-one"}, {"result": "private-two"}, because="changed")
        assert_observation((1, "ready"), (1, "ready"), because="legacy tuple")
    assert len(inner) == len(outer) == 1
    assert not inner[0].matched and inner[0].fields == ["result"]
    assert outer[0].matched and outer[0].fields == ["<value>"]
    # Outside recording the assertion must still run; the prior record stays closed.
    with pytest.raises(AssertionError, match="outside"):
        assert_observation(1, 2, because="outside")
    assert len(outer) == 1


def test_pydantic_observation_uses_model_fields_without_serializing_values() -> None:
    pydantic = pytest.importorskip("pydantic")
    BaseModel, Field = pydantic.BaseModel, pydantic.Field

    class Publication(BaseModel):
        event_id: str
        # Serialization exclusion does not exclude this field from model equality.
        access_token: str = Field(exclude=True)

    with record_observations() as checks, pytest.raises(AssertionError, match="publication"):
        assert_observation(
            Publication(event_id="private-event", access_token="private-before"),
            Publication(event_id="private-event", access_token="private-after"),
            because="publication",
        )
    assert checks[0].fields == ["event_id", "access_token"]
    assert not checks[0].matched
    assert "private-" not in repr(checks)
