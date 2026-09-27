"""A changed copy of a declaration passes the same checks as a new one."""

import pytest
from pydantic import ValidationError

from due_work_harness import DueWorkContractDesignError, DueWorkSource


def place() -> None: ...


def refund() -> None: ...


def test_a_copy_with_a_valid_change_is_the_changed_value() -> None:
    source = DueWorkSource(place)
    assert source.model_copy(update={"callable": refund, "sites": 2}) == DueWorkSource(refund, sites=2)
    assert source.model_copy() == source


def test_a_copy_that_breaks_a_design_rule_is_refused() -> None:
    with pytest.raises(DueWorkContractDesignError, match="sites must be at least one"):
        DueWorkSource(place).model_copy(update={"sites": 0})


def test_a_copy_that_names_no_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="site"):
        DueWorkSource(place).model_copy(update={"site": 2})
