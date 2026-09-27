"""
The delegation tripwire follows names through production service objects, and no further.

An adapter often reaches production through a module-level service instance
(``feed.attempt_execution(pk)``). That must count as delegation; a test-owned
object with the same method name must not, and neither may reading a data
attribute off the production object.
"""

import pytest
from sample_production.feed import event_feed

from due_work_harness.binding import assert_test_binding_delegates_to_production

pytestmark = pytest.mark.usefixtures("production_host")


def test_named_method_on_production_capability_is_recognized() -> None:
    def execute():
        return event_feed.attempt_execution(1)

    # Only inspect the binding. The behavioral proofs separately execute it.
    assert_test_binding_delegates_to_production(
        adopter="production instance", field="execute", binding=execute, production_shape="execution"
    )


def test_named_method_on_test_owned_instance_is_still_rejected() -> None:
    class PretendCapability:
        def attempt_execution(self, pk):
            return pk

    pretend = PretendCapability()

    def execute():
        return pretend.attempt_execution(1)

    with pytest.raises(AssertionError, match="references no production"):
        assert_test_binding_delegates_to_production(
            adopter="test instance", field="execute", binding=execute, production_shape="execution"
        )


def test_referencing_production_data_is_not_production_execution() -> None:
    policy = event_feed.policy

    def pretend_execute():
        return policy.max_rearms

    with pytest.raises(AssertionError, match="references no production"):
        assert_test_binding_delegates_to_production(
            adopter="data only", field="execute", binding=pretend_execute, production_shape="execution"
        )
