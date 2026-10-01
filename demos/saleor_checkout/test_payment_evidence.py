"""Payment-provider facts through Saleor's actual event-report mutation.

The oracle counts provider money and durable event identities, not only Saleor's
cached totals. Each generated history creates a new transaction. Redelivery uses
identical provider references and event timestamps, as real webhook retries do.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from copy import copy
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from itertools import combinations
from threading import Event

import pytest
from django.db import connection, connections
from django.utils import timezone
from saleor.graphql.core import ResolveInfo
from saleor.graphql.payment.mutations.transaction.transaction_event_report import TransactionEventReport
from saleor.payment import TransactionEventType
from saleor.payment.models import TransactionItem
from test_order_confirmation import _checkout, current_shop, saleor_shop  # noqa: F401

from pytest_obligation import (
    Adoption,
    CallableDelivery,
    Claim,
    Findings,
    HandoffHistory,
    KnownGap,
    NotApplicable,
    ObligationContract,
    Profile,
    due_work_contract_suite,
    due_work_database,
)
from pytest_obligation.crash_histories import assert_crash_at_every_commit_converges
from pytest_obligation.interleavings import EvidenceConfluence, EvidenceExpectation, EvidenceSession
from pytest_obligation.interleavings.model import InterleavingFailure, require
from pytest_obligation.models import HarnessModel
from pytest_obligation.profiles.catalog import ConvergenceFamily

INFO: ContextVar[ResolveInfo] = ContextVar("saleor_payment_report_info")


@pytest.fixture
def payment_reporter(info: ResolveInfo) -> Iterator[None]:
    """Use upstream resolver context; transaction ownership is still checked by the mutation."""
    info.context.user = current_shop().customer
    token = INFO.set(info)
    yield
    INFO.reset(token)


class Money(HarnessModel):
    """Independently read projection and ledger; duplicates must not create new event identities."""

    charged: Decimal
    refunded: Decimal
    events: int


FACTS = {
    "charge-seven": (TransactionEventType.CHARGE_SUCCESS, Decimal(7)),
    "charge-eleven": (TransactionEventType.CHARGE_SUCCESS, Decimal(11)),
    "refund-five": (TransactionEventType.REFUND_SUCCESS, Decimal(5)),
}


def report(transaction: TransactionItem, alias: str, at: datetime) -> None:
    """Production resolver, with an unchanged external provider event on every redelivery."""
    event_type, amount = FACTS[alias]
    info = copy(INFO.get())
    info.context = copy(info.context)
    info.context.dataloaders = {}  # Each provider HTTP request owns its loaders, including separate actors.
    TransactionEventReport.perform_mutation(
        None,
        info,
        token=transaction.token,
        type=event_type,
        psp_reference=alias,
        amount=amount,
        time=at,
    )


def money(transaction: TransactionItem) -> Money:
    transaction.refresh_from_db()
    return Money(
        charged=transaction.charged_value, refunded=transaction.refunded_value, events=transaction.events.count()
    )


def unpaid_transaction() -> TransactionItem:
    checkout, _total = _checkout()
    return current_shop().transaction_item(checkout_id=checkout.pk, user=current_shop().customer)


@contextmanager
def actor_connection() -> Iterator[None]:
    """Each ordered actor owns its real DB connection, including cleanup after a failure."""
    try:
        yield
    finally:
        connections.close_all()


@contextmanager
def payment_evidence() -> Iterator[EvidenceSession[Money]]:
    # ARRANGE: a real checkout and unpaid transaction, with fixed provider event timestamps.
    # REAL PRODUCTION: TransactionEventReport performs deduplication, money calculation and checkout updates.
    # EXTERNAL SEAM: only provider facts; neither totals nor outcome rows are written by this adapter.
    # OBSERVE: independently reread money totals and count durable event identities.
    transaction = unpaid_transaction()
    at = timezone.now()
    delivered: dict[str, datetime] = {}

    def deliver(alias: str, moment: datetime) -> None:
        delivered[alias] = moment  # Provider resend ledger; never an application transition.
        report(transaction, alias, moment)

    facts = {name: partial(deliver, name, at + timedelta(seconds=index)) for index, name in enumerate(FACTS)}
    expectations = {}
    for size in range(len(FACTS) + 1):
        for subset in combinations(FACTS, size):
            seen = frozenset(subset)
            charged = sum((FACTS[name][1] for name in seen if name.startswith("charge")), Decimal(0))
            refunded = Decimal(5) if "refund-five" in seen else Decimal(0)
            expectations[seen] = EvidenceExpectation(
                observation=Money(charged=charged - refunded, refunded=refunded, events=len(seen)),
                effects={},
            )

    # Recovery is provider redelivery of already accepted reports, not a test-side recalculation.
    # Duplicate processing must preserve the same ledger and money without inventing additional facts.
    def recover() -> None:
        for alias, moment in delivered.items():
            report(transaction, alias, moment)

    try:
        yield EvidenceSession(
            prepare=lambda: None,
            facts=facts,
            observe=lambda: money(transaction),
            expectations=expectations,
            recover=recover,
            advance=lambda _seconds: None,
            effects=lambda: {},
            actor_scope=actor_connection,
        )
    finally:
        transaction.delete()


PAYMENT_EVIDENCE = EvidenceConfluence(
    name="payment provider reports",
    bind=payment_evidence,
    facts=tuple(FACTS),
    dependencies=(("charge-seven", "refund-five"),),
    ordered_pair=("charge-seven", "charge-eleven"),
    no_retry_because="These are incoming provider reports; issuing a new payment is a separate mutation.",
)

# Callback replay is the provider's actual recovery behavior, not a fabricated repair loop.
REPORTS: list[tuple[TransactionItem, str, datetime]] = []


def charge_once(transaction: TransactionItem) -> None:
    at = timezone.now()
    REPORTS.append((transaction, "charge-seven", at))
    report(transaction, "charge-seven", at)


def redeliver() -> None:
    for transaction, alias, at in REPORTS:
        report(transaction, alias, at)


def new_charge() -> TransactionItem:
    REPORTS.clear()
    return unpaid_transaction()


CHARGE_HISTORY = HandoffHistory(
    name="charge event and money projection",
    arrange=new_charge,
    transition=charge_once,
    observe=money,
    findings=Findings(
        Money(charged=Decimal(7), refunded=Decimal(0), events=1),
        {"worker died after commit 1": Money(charged=Decimal(0), refunded=Decimal(0), events=1)},
    ),
)
PROVIDER_REDELIVERY = CallableDelivery(name="payment provider redelivery", recover=redeliver)


PAYMENT_REPORTS = ObligationContract(
    name="saleor payment event reports",
    adoption=Adoption.LEGACY,
    transactional=True,
    fixtures=("saleor_shop", "payment_reporter"),
    profiles={
        **{
            profile: NotApplicable(
                "This contract scopes incoming payment evidence, not a worker or outgoing payment command."
            )
            for profile in Profile
        },
        Profile.E: Claim(),
        Profile.I: KnownGap(
            "The event commits before its money projection. After death at that boundary, duplicate detection "
            "accepts redelivery without repairing the transaction total.",
            detect=partial(assert_crash_at_every_commit_converges, PROVIDER_REDELIVERY, CHARGE_HISTORY),
        ),
    },
    evidence_confluence={PAYMENT_EVIDENCE.name: PAYMENT_EVIDENCE},
    convergence_families={
        ConvergenceFamily.STALE_SNAPSHOTS: NotApplicable(
            "The resolver rereads transaction events; it accepts no worker snapshot."
        ),
        ConvergenceFamily.MONOTONIC_RESULTS: NotApplicable(
            "Charges and refunds legitimately change the amount; the fact-set oracle covers their meaning."
        ),
        ConvergenceFamily.IN_FLIGHT: NotApplicable(
            "No outgoing provider operation occurs in this incoming report resolver."
        ),
    },
)


@due_work_contract_suite(PAYMENT_REPORTS)
class TestPaymentEventReports:
    """Generated permutations, duplicate reports, recovery, separate actors and commit deaths."""


@pytest.mark.xfail(
    strict=True,
    raises=InterleavingFailure,
    reason="A slower payment report overwrites the newer money aggregate; duplicate redelivery does not repair it.",
)
@pytest.mark.usefixtures("saleor_shop", "payment_reporter")
@due_work_database(transactional=True)
def test_overlapping_payment_reports_preserve_all_provider_money() -> None:
    """Two independent requests must not publish an older aggregate over a newer one.

    Unlike ordered actors, these transactions overlap: the first report has
    computed its projection and waits before writing; the second report fully
    commits. Only scheduling is controlled, through the actual SQL boundary.
    """
    transaction = unpaid_transaction()
    at = timezone.now()
    projection_ready, release_projection = Event(), Event()

    def first_request() -> None:
        def before_write(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            if sql.startswith('UPDATE "payment_transactionitem"') and not projection_ready.is_set():
                projection_ready.set()
                assert release_projection.wait(10), "second payment request never released the first projection"
            return execute(sql, params, many, context)

        with actor_connection(), connection.execute_wrapper(before_write):
            report(transaction, "charge-seven", at)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(copy_context().run, first_request)
        try:
            assert projection_ready.wait(10), "first report never reached its money UPDATE"
            report(transaction, "charge-eleven", at + timedelta(seconds=1))
        finally:
            release_projection.set()
        first.result(timeout=10)

    # A provider retry is the real recovery route; it must not leave stale totals behind.
    report(transaction, "charge-seven", at)
    report(transaction, "charge-eleven", at + timedelta(seconds=1))
    observed = money(transaction)
    require(
        observed == Money(charged=Decimal(18), refunded=Decimal(0), events=2),
        "money",
        f"Two accepted charges must total 18 after redelivery; observed {observed}",
    )
