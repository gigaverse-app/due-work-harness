"""
Saleor's checkout, unmodified, adopted as a project adopts the harness.

Upstream: saleor @ 5ff56489 (``saleor/checkout/complete_checkout.py``).
Completing a paid checkout makes three commits before any callback runs:
reserving stock, capturing the payment, then creating the order. The order's
transaction registers two ``transaction.on_commit`` callbacks: ``order_created``
(the order's events, the customer's order count, the ORDER_CREATED webhooks),
whose writes each commit on their own, and ``send_order_confirmation``. The
obligation: a customer who is charged has an order, and every order is recorded
as placed and confirmed to them.

Two contracts, each a declaration and one decorated class, as an adopter
writes them. ``CHECKOUT_AS_SHIPPED`` covers the two ``on_commit`` handoffs the
scan finds in ``_post_create_order_actions`` and records what the harness finds
as legacy gaps, each a strict xfail; ``due-work-harness check`` counts it (see
``pyproject.toml``, whose baseline holds Saleor's other handoffs).
``CHECKOUT_WITH_AUTOMATIC_COMPLETION`` is Saleor's own answer to the charged-
but-no-order finding, and passes. ``test_what_each_failure_costs_the_customer``
pins what every history leaves behind, so a change upstream or in the harness
shows exactly what moved.

The transition is ``complete_checkout``, called exactly as Saleor's
``checkoutComplete`` mutation calls it. (Not through the GraphQL view: its
graphql-core 2 executor waits on a promise that a simulated death, a
``BaseException`` raised in the resolver, never resolves. A real process death
never crosses the resolver, so this is an artefact of simulating it, and the
mutation adds nothing after ``complete_checkout`` returns.) The fixtures are
Saleor's own. Recovery is everything Saleor does on its own: every task in its
``CELERY_BEAT_SCHEDULE``, run at one hour, one day, 31 days and 91 days later,
past every expiry Saleor configures.
"""

from collections import Counter
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.conf import settings
from django.contrib.sites.models import Site
from django.utils import timezone
from freezegun import freeze_time
from pydantic import BaseModel, ConfigDict
from saleor.account.models import Address, User
from saleor.celeryconf import app
from saleor.channel.models import Channel
from saleor.checkout import calculations
from saleor.checkout.complete_checkout import _post_create_order_actions, complete_checkout
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutMetadata
from saleor.checkout.payment_utils import update_checkout_payment_statuses
from saleor.checkout.tasks import delete_expired_checkouts
from saleor.checkout.tests.utils import add_variant_to_checkout
from saleor.core.notify import NotifyEventType
from saleor.graphql.core.utils import to_global_id_or_none
from saleor.order.models import Order
from saleor.payment import ChargeStatus, TransactionEventType
from saleor.payment.models import Payment, TransactionItem
from saleor.payment.utils import recalculate_transaction_amounts
from saleor.plugins.manager import PluginsManager, get_plugins_manager
from saleor.product.models import ProductVariant
from saleor.site.models import SiteSettings
from saleor.warehouse.models import Stock

from due_work_harness import (
    Adoption,
    CallableDelivery,
    Claim,
    Decline,
    DueWorkContract,
    DueWorkSource,
    HandoffHistory,
    KnownGap,
    NotApplicable,
    Profile,
    Retention,
    SafetyContract,
    SafetyProfile,
    due_work_contract_suite,
)
from due_work_harness.crash_histories import assert_histories_converge, crash_histories

#: Far enough ahead for every age-based task Saleor schedules to act: the longest,
#: deleting a user's checkout, waits USER_CHECKOUTS_TIMEDELTA (90 days).
RECOVERY_HORIZONS = (timedelta(hours=1), timedelta(days=1), timedelta(days=31), timedelta(days=91))


def run_beat_schedule() -> None:
    """What Saleor does on its own over the following three months: every periodic task, at each horizon."""
    app.loader.import_default_modules()
    start = timezone.now()
    for horizon in RECOVERY_HORIZONS:
        with freeze_time(start + horizon):
            for entry in settings.CELERY_BEAT_SCHEDULE.values():
                app.tasks[entry["task"]].apply()


# The test settings run Celery tasks eagerly and Saleor's callbacks run in the
# web process: there is no message separate from the process, so no `lose`.
SALEOR = CallableDelivery(name="saleor complete_checkout", recover=run_beat_schedule)


class Shop(BaseModel):
    """What Saleor's own fixtures built for one test, published for the contract's zero-argument bindings."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    channel: Channel
    variant: ProductVariant
    customer: User
    address: Address
    assign_delivery: Callable[[Checkout], Any]
    #: Order confirmations Saleor handed to its notification plugins, by order id.
    confirmations: Counter[str]
    #: Saleor's own Transactions API fixture factories, for the paid-checkout history.
    transaction_item: Callable[..., TransactionItem]
    transaction_events: Callable[..., Any]


_SHOPS: list[Shop] = []


def current_shop() -> Shop:
    assert _SHOPS, "a Saleor contract case runs inside its saleor_shop fixture"
    return _SHOPS[-1]


@pytest.fixture
def saleor_shop(
    monkeypatch: pytest.MonkeyPatch,
    channel_USD,  # noqa: ANN001, N803
    product,  # noqa: ANN001
    shipping_zone,  # noqa: ANN001, ARG001
    customer_user,  # noqa: ANN001
    address,  # noqa: ANN001
    checkout_delivery,  # noqa: ANN001
    transaction_item_generator,  # noqa: ANN001
    transaction_events_generator,  # noqa: ANN001
) -> Iterator[Shop]:
    """Saleor's own fixtures, with the product stocked for every history's order and confirmations counted."""
    channel_USD.automatically_confirm_all_new_orders = True
    channel_USD.save()
    variant = product.variants.first()
    # ARRANGE: every history places an order, so stock the variant for all of them.
    Stock.objects.filter(product_variant=variant).update(quantity=1_000_000)
    shop = Shop(
        channel=channel_USD,
        variant=variant,
        customer=customer_user,
        address=address,
        assign_delivery=checkout_delivery,
        confirmations=Counter(),
        transaction_item=transaction_item_generator,
        transaction_events=transaction_events_generator,
    )
    notify = PluginsManager.notify

    def recording_notify(self: PluginsManager, event: str, payload_func, channel_slug=None, **kwargs):  # noqa: ANN001, ANN202
        # EXTERNAL SEAM: the call Saleor's own checkout tests mock; plugins would send the email from here.
        # Recorded into the shop's own counter: Pydantic validated a copy of any counter passed in.
        if event == NotifyEventType.ORDER_CONFIRMATION:
            shop.confirmations[payload_func()["order"]["id"]] += 1
        return notify(self, event, payload_func=payload_func, channel_slug=channel_slug, **kwargs)

    monkeypatch.setattr(PluginsManager, "notify", recording_notify)
    _SHOPS.append(shop)
    yield shop
    _SHOPS.pop()


@pytest.fixture
def automatic_completion(saleor_shop: Shop) -> None:
    """The channel opts into Saleor's automatic completion of fully paid checkouts, with no delay."""
    saleor_shop.channel.automatically_complete_fully_paid_checkouts = True
    saleor_shop.channel.automatic_completion_delay = 0
    saleor_shop.channel.save()


type Handle = tuple[UUID, Any]


def _checkout() -> tuple[Checkout, Any]:
    """A customer's checkout for one product, as Saleor's own checkoutComplete tests arrange it."""
    shop = current_shop()
    checkout = Checkout.objects.create(
        currency=shop.channel.currency_code,
        channel=shop.channel,
        price_expiration=timezone.now() + settings.CHECKOUT_PRICES_TTL,
        email=shop.customer.email,
        user=shop.customer,
        shipping_address=shop.address.get_copy(),
        billing_address=shop.address.get_copy(),
    )
    checkout.set_country("US", commit=True)
    CheckoutMetadata.objects.create(checkout=checkout)
    manager = get_plugins_manager(allow_replica=False)
    add_variant_to_checkout(fetch_checkout_info(checkout, [], manager), shop.variant, 1)
    checkout.assigned_delivery = shop.assign_delivery(checkout)
    checkout.save()
    lines, _ = fetch_checkout_lines(checkout)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, fetch_checkout_info(checkout, lines, manager), lines
    )
    return checkout, total.gross


def complete(handle: Handle) -> None:
    """REAL PRODUCTION: complete_checkout, called as Saleor's checkoutComplete mutation calls it."""
    manager = get_plugins_manager(allow_replica=False)
    checkout = Checkout.objects.get(pk=handle[0])
    lines, _ = fetch_checkout_lines(checkout)
    complete_checkout(
        manager=manager,
        checkout_info=fetch_checkout_info(checkout, lines, manager),
        lines=lines,
        payment_data={},
        store_source=False,
        user=current_shop().customer,
        app=None,
        site_settings=SiteSettings.objects.get(site=Site.objects.get_current()),
        redirect_url="https://www.example.com",
    )


def _order(checkout_pk: UUID) -> Order | None:
    return Order.objects.filter(checkout_token=str(checkout_pk)).first()


def _money_on(model: type[Payment] | type[TransactionItem], pk: object) -> str:
    """What a payment's money belongs to: its order, its checkout, or nothing at all."""
    order_id, checkout_id = model.objects.filter(pk=pk).values_list("order_id", "checkout_id").get()
    return "order" if order_id else "checkout" if checkout_id else "nothing"


class Outcome(BaseModel):
    """What the customer and the shop see: the order, its history, the confirmation, and the money."""

    model_config = ConfigDict(frozen=True)

    order: bool
    events: tuple[str, ...]
    confirmations: int
    charged: bool
    #: What the customer's money belongs to: "order", "checkout" or "nothing".
    money_on: str


def pay_with_the_payments_api() -> Handle:
    """ARRANGE: a fresh checkout and its Payments API payment, which complete_checkout captures itself."""
    checkout, total = _checkout()
    payment = Payment.objects.create(
        gateway="mirumee.payments.dummy",
        checkout=checkout,
        is_active=True,
        total=total.amount,
        currency=total.currency,
        billing_email=checkout.email,
    )
    return checkout.pk, payment.pk


def order_confirmation_and_money(handle: Handle) -> Outcome:
    """OBSERVE: the order and its history, the confirmations the customer was sent, and where the money is."""
    order = _order(handle[0])
    payment = Payment.objects.get(pk=handle[1])
    order_id = to_global_id_or_none(order) if order else None
    return Outcome(
        order=order is not None,
        events=tuple(sorted(order.events.values_list("type", flat=True))) if order else (),
        confirmations=current_shop().confirmations[order_id] if order_id else 0,
        charged=payment.captured_amount > 0,
        money_on=_money_on(Payment, payment.pk),
    )


COMPLETE_CHECKOUT = HandoffHistory(
    name="complete checkout",
    arrange=pay_with_the_payments_api,
    transition=complete,
    observe=order_confirmation_and_money,
)

#: Past USER_CHECKOUTS_TIMEDELTA (90 days), after which Saleor deletes a user's checkout.
EXPIRED = timedelta(days=91)


def _expire(checkout: Checkout) -> UUID:
    # ARRANGE: untouched since before Saleor's expiry window (update() skips last_change's auto_now).
    Checkout.objects.filter(pk=checkout.pk).update(last_change=timezone.now() - EXPIRED)
    return checkout.pk


def charged_through_the_payments_api() -> UUID:
    """A checkout whose Payments API payment was captured, and whose order was never created: an order is owed."""
    checkout, total = _checkout()
    Payment.objects.create(
        gateway="mirumee.payments.dummy",
        checkout=checkout,
        is_active=True,
        total=total.amount,
        captured_amount=total.amount,
        charge_status=ChargeStatus.FULLY_CHARGED,
        currency=total.currency,
        billing_email=checkout.email,
    )
    return _expire(checkout)


def charged_through_transactions() -> UUID:
    """A checkout charged through the Transactions API, and whose order was never created: an order is owed."""
    checkout_pk, _ = pay_with_transactions()
    return _expire(Checkout.objects.get(pk=checkout_pk))


def abandoned() -> UUID:
    """A checkout nobody paid for, past its expiry: Saleor's own policy says it goes."""
    checkout, _ = _checkout()
    return _expire(checkout)


def _checkout_retention(owed: Callable[[], UUID]) -> Retention:
    return Retention(
        name="saleor delete_expired_checkouts",
        make_non_terminal=owed,
        make_prunable=abandoned,
        run_retention=delete_expired_checkouts,
        still_exists=lambda checkout_pk: Checkout.objects.filter(pk=checkout_pk).exists(),
    )


def retention_of_payments_api_checkouts() -> Retention:
    # ARRANGE: an expired checkout holding a captured Payments API payment, and an abandoned one.
    # REAL PRODUCTION: Saleor's delete_expired_checkouts task, as its beat schedule runs it.
    # EXTERNAL SEAM: none.
    # OBSERVE: whether each checkout still exists.
    return _checkout_retention(charged_through_the_payments_api)


def retention_of_transactions_checkouts() -> Retention:
    # ARRANGE: an expired checkout holding charged Transactions API money, and an abandoned one.
    # REAL PRODUCTION: Saleor's delete_expired_checkouts task, as its beat schedule runs it.
    # EXTERNAL SEAM: none.
    # OBSERVE: whether each checkout still exists.
    return _checkout_retention(charged_through_transactions)


CHECKOUT_AS_SHIPPED = DueWorkContract(
    name="saleor checkout",
    adoption=Adoption.LEGACY,
    transactional=True,
    fixtures=("saleor_shop",),
    profiles={
        Profile.A: KnownGap(
            "no periodic task Saleor schedules selects an order whose after-commit work never ran: its events, "
            "its ORDER_CREATED webhooks and its confirmation are owed by nothing but the lost callbacks"
        ),
        Profile.B: Decline(
            "completion serialises on the checkout row lock (select_for_update) inside each of its transactions; "
            "it holds no lease that could outlive a worker"
        ),
        Profile.C: KnownGap(
            "with the Payments API the gateway captures the payment between two commits: a death after the "
            "capture leaves an outcome Saleor never reconciles, so the customer is charged and no order is made"
        ),
        Profile.D: Claim(
            gaps={
                "assert_retention_preserves_non_terminal_work": (
                    "delete_expired_checkouts keeps checkouts holding Transactions API money, but deletes one "
                    "holding a captured Payments API payment, the only record that an order is owed: after 90 "
                    "days that payment belongs to nothing"
                )
            }
        ),
        Profile.E: NotApplicable("one completion writes each order's events and confirmation once; no two race"),
        Profile.F: KnownGap(
            "no product state records that an order is still to be confirmed, so no recovery can derive the "
            "obligation the lost callback held"
        ),
    },
    safety=SafetyContract(
        name="saleor checkout",
        adoption=Adoption.LEGACY,
        profiles={
            SafetyProfile.REPLAY_SAFE_EXECUTION: Decline(
                "nothing in Saleor replays a lost confirmation (profile A), so there is no replay to make safe"
            ),
            SafetyProfile.BOUNDED_RETRY: NotApplicable("the post-commit callbacks are not retried"),
        },
    ),
    retention=retention_of_payments_api_checkouts,
    handoffs=(COMPLETE_CHECKOUT,),
    handoff_delivery=SALEOR,
    handoff_gaps={
        "complete checkout": (
            "a death after the Payments API capture charges the customer with no order, which after 90 days "
            "belongs to nothing; a death after the order commits leaves it unconfirmed with its history empty or "
            "half-written; and a failing order_created callback, with no death at all, makes Django skip the "
            "confirmation. test_what_each_failure_costs_the_customer pins each history"
        )
    },
)


@due_work_contract_suite(CHECKOUT_AS_SHIPPED, covers=(DueWorkSource(_post_create_order_actions, sites=2),))
class TestCheckoutAsShipped:
    pass


PLACED = Outcome(
    order=True,
    events=("confirmed", "order_fully_paid", "payment_captured", "placed"),
    confirmations=1,
    charged=True,
    money_on="order",
)


def _outcome(**changes: object) -> Outcome:
    return PLACED.model_copy(update=changes)


#: What each history leaves after three months of everything Saleor schedules.
#: Every entry but the first is a loss the customer or the shop sees.
FINDINGS = {
    # Benign: nothing was charged, and the customer can try again.
    "worker died after commit 1": _outcome(order=False, events=(), confirmations=0, charged=False, money_on="nothing"),
    # FINDING 1: charged, and no order is ever created. The payment is captured in its
    # own transaction before the order's; 90 days later Saleor deletes the checkout,
    # and the captured payment belongs to nothing.
    "worker died after commit 2": _outcome(order=False, events=(), confirmations=0, money_on="nothing"),
    # FINDING 2: a paid order, never confirmed, its history half-written or empty.
    # Everything after the order's commit runs in on_commit callbacks, one
    # autocommit write at a time, and nothing Saleor schedules re-runs them.
    "worker died after commit 3": _outcome(events=(), confirmations=0),
    "worker died after commit 4": _outcome(events=(), confirmations=0),
    "worker died after commit 5": _outcome(events=(), confirmations=0),
    "worker died after commit 6": _outcome(events=("placed",), confirmations=0),
    "worker died after commit 7": _outcome(events=("payment_captured", "placed"), confirmations=0),
    "worker died after commit 8": _outcome(events=("order_fully_paid", "payment_captured", "placed"), confirmations=0),
    "worker died after commit 9": _outcome(events=("order_fully_paid", "payment_captured", "placed"), confirmations=0),
    "worker died after commit 10": _outcome(confirmations=0),
    # FINDING 3: no death at all. When order_created raises (a webhook payload bug, a
    # plugin error, a database error), Django skips every later callback of the
    # commit, so the confirmation is lost with the order's history.
    "after-commit callback 1 failed": _outcome(events=(), confirmations=0),
    "after-commit callback 2 failed": _outcome(confirmations=0),
}


@pytest.mark.django_db(transaction=True)
def test_what_each_failure_costs_the_customer(saleor_shop: Shop) -> None:  # noqa: ARG001
    runs = crash_histories(SALEOR, COMPLETE_CHECKOUT)
    assert runs[0].after == PLACED, "normal operation places, records and confirms the order"
    assert {run.label: run.after for run in runs[1:]} == FINDINGS
    with pytest.raises(AssertionError, match="Work was lost or repeated"):
        assert_histories_converge(SALEOR.name, runs)


class Paid(BaseModel):
    """The money and the order alone: whether a charged customer has an order to show for it."""

    model_config = ConfigDict(frozen=True)

    order: bool
    charged: bool
    money_on: str


def pay_with_transactions() -> Handle:
    """ARRANGE: a fresh checkout, charged by the payment app before completion, as Saleor's own tests arrange it."""
    shop = current_shop()
    checkout, total = _checkout()
    transaction = shop.transaction_item(checkout_id=checkout.pk)
    shop.transaction_events(
        transaction=transaction,
        psp_references=["1"],
        types=[TransactionEventType.CHARGE_SUCCESS],
        amounts=[total.amount],
    )
    recalculate_transaction_amounts(transaction)
    # Settle the checkout's payment status now, as Saleor would on the next fetch, so that
    # completing it writes nothing inside fetch_checkout_data's promise chain, where a
    # simulated death (a BaseException) would leave the promise waiting forever.
    update_checkout_payment_statuses(checkout, total, checkout_has_lines=True)
    return checkout.pk, transaction.pk


def order_and_money(handle: Handle) -> Paid:
    """OBSERVE: whether the charged customer has an order, and where the money is."""
    transaction = TransactionItem.objects.get(pk=handle[1])
    return Paid(
        order=_order(handle[0]) is not None,
        charged=transaction.charged_value > Decimal(0),
        money_on=_money_on(TransactionItem, transaction.pk),
    )


COMPLETE_PAID_CHECKOUT = HandoffHistory(
    name="complete paid checkout",
    arrange=pay_with_transactions,
    transition=complete,
    observe=order_and_money,
)

CHECKOUT_WITH_AUTOMATIC_COMPLETION = DueWorkContract(
    name="saleor checkout, Transactions API with automatic completion",
    transactional=True,
    fixtures=("saleor_shop", "automatic_completion"),
    profiles={
        Profile.A: Decline(
            "Saleor's recovery is trigger_automatic_checkout_completion_task, which the handoff history below "
            "runs among the beat tasks and proves end to end. Its selection is built inline in the task, so no "
            "sweep can be bound without restating the query in a test; extracting it into a callable would let "
            "this contract claim profile A"
        ),
        Profile.B: Decline("completion serialises on the checkout row lock; it holds no lease"),
        Profile.C: Decline(
            "the payment app charges before completion and records a TransactionItem, so completion makes no "
            "gateway call whose outcome could be unknown"
        ),
        Profile.D: Claim(),
        Profile.E: NotApplicable("one completion writes each order once; no two race"),
        Profile.F: Decline(
            "the obligation is the fully paid checkout itself, which automatic completion selects from product state"
        ),
    },
    safety=SafetyContract(
        name="saleor checkout, Transactions API with automatic completion",
        profiles={
            SafetyProfile.REPLAY_SAFE_EXECUTION: Decline(
                "completing an already completed checkout returns its existing order"
            ),
            SafetyProfile.BOUNDED_RETRY: NotApplicable("automatic completion retries on the beat schedule, unbounded"),
        },
    ),
    retention=retention_of_transactions_checkouts,
    handoffs=(COMPLETE_PAID_CHECKOUT,),
    handoff_delivery=SALEOR,
)


@due_work_contract_suite(CHECKOUT_WITH_AUTOMATIC_COMPLETION)
class TestCheckoutWithAutomaticCompletion:
    """Saleor's own design: money held as a transaction, and a beat task that completes a paid checkout."""
