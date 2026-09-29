"""Source for the child pytest sessions that the findings tests run."""

SUITE = """
    from due_work_harness import Findings, configure
    from due_work_harness.contract import Adoption, NotApplicable, Profile, SafetyContract, SafetyProfile
    from due_work_harness.contract import DueWorkContract, due_work_contract_suite
    from due_work_harness.crash_histories import HandoffHistory
    from due_work_harness.host import Host
    from due_work_harness.references import in_memory_handoffs as ref

    configure(Host(worker_killer=ref.ledger_killer))
    WHY = "the successor is committed separately"
    NA = NotApplicable("self-test")

    CONTRACT = DueWorkContract(
        name="split handoff",
        adoption=Adoption.LEGACY,
        profiles={{profile: NA for profile in Profile}},
        safety=SafetyContract(name="split handoff", profiles={{profile: NA for profile in SafetyProfile}}),
        handoffs=(
            HandoffHistory(
                name="retryable failure",
                arrange=ref.running_attempt,
                transition=ref.fail_with_split_handoff,
                observe=ref.attempt_and_successors,
                findings=Findings(("retryable_failed", ("running",)), {{{pinned!r}: ("retryable_failed", ())}}),
            ),
        ),
        handoff_delivery=ref.RETRY_DELIVERY,
        handoff_gaps={{"retryable failure": WHY}},
    )

    @due_work_contract_suite(CONTRACT)
    class TestSplit:
        pass
"""
