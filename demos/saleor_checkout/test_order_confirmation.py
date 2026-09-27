"""
The harness against Saleor's checkout, unmodified.

Upstream: saleor @ 5ff56489 (``saleor/checkout/complete_checkout.py``).
Completing a paid checkout makes three commits before any callback runs:
reserving stock, capturing the payment, then creating the order. The order's
transaction registers two ``transaction.on_commit`` callbacks: ``order_created``
(the order's events, the customer's order count, the ORDER_CREATED webhooks),
whose writes each commit on their own, and ``send_order_confirmation``. The
obligation: a customer who is charged has an order, and every order is recorded
as placed and confirmed to them.

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
from collections.abc import Callable
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
from saleor.celeryconf import app
from saleor.checkout import calculations
from saleor.checkout.complete_checkout import complete_checkout
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutMetadata
from saleor.checkout.payment_utils import update_checkout_payment_statuses
from saleor.checkout.tests.utils import add_variant_to_checkout
from saleor.core.notify import NotifyEventType
from saleor.graphql.core.utils import to_global_id_or_none
from saleor.order.models import Order
from saleor.payment import TransactionEventType
from saleor.payment.models import Payment, TransactionItem
from saleor.payment.utils import recalculate_transaction_amounts
from saleor.plugins.manager import PluginsManager, get_plugins_manager
from saleor.site.models import SiteSettings
from saleor.warehouse.models import Stock

from due_work_harness import CallableDelivery, HandoffHistory, assert_crash_at_every_commit_converges
from due_work_harness.crash_histories import assert_histories_converge, crash_histories

pytestmark = pytest.mark.django_db(transaction=True)

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


class Outcome(BaseModel):
    """What the customer and the shop see: the order, its history, the confirmation, and the money."""

    model_config = ConfigDict(frozen=True)

    order: bool
    events: tuple[str, ...]
    confirmations: int
    charged: bool
    #: What the customer's money belongs to: "order", "checkout" or "nothing".
    money_on: str


class Paid(BaseModel):
    """The money and the order alone: whether a charged customer has an order to show for it."""

    model_config = ConfigDict(frozen=True)

    order: bool
    charged: bool
    money_on: str


type Handle = tuple[UUID, Any]


@pytest.fixture
def confirmations(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Order confirmations Saleor hands to its notification plugins, by order id."""
    sent: Counter[str] = Counter()
    notify = PluginsManager.notify

    def recording_notify(self: PluginsManager, event: str, payload_func, channel_slug=None, **kwargs):  # noqa: ANN001, ANN202
        # EXTERNAL SEAM: the call Saleor's own checkout tests mock; plugins would send the email from here.
        if event == NotifyEventType.ORDER_CONFIRMATION:
            sent[payload_func()["order"]["id"]] += 1
        return notify(self, event, payload_func=payload_func, channel_slug=channel_slug, **kwargs)

    monkeypatch.setattr(PluginsManager, "notify", recording_notify)
    return sent


@pytest.fixture
def shop(channel_USD, product, shipping_zone, customer_user, address, checkout_delivery):  # noqa: ANN001, ANN201, N803
    """Saleor's own fixtures, with the product stocked for every history's order."""
    channel_USD.automatically_confirm_all_new_orders = True
    channel_USD.save()
    variant = product.variants.first()
    # ARRANGE: every history places an order, so stock the variant for all of them.
    Stock.objects.filter(product_variant=variant).update(quantity=1_000_000)
    return channel_USD, variant, customer_user, address, checkout_delivery


def _checkout(shop) -> tuple[Checkout, Any]:  # noqa: ANN001
    """A customer's checkout for one product, as Saleor's own checkoutComplete tests arrange it."""
    channel, variant, customer, address, checkout_delivery = shop
    checkout = Checkout.objects.create(
        currency=channel.currency_code,
        channel=channel,
        price_expiration=timezone.now() + settings.CHECKOUT_PRICES_TTL,
        email=customer.email,
        user=customer,
        shipping_address=address.get_copy(),
        billing_address=address.get_copy(),
    )
    checkout.set_country("US", commit=True)
    CheckoutMetadata.objects.create(checkout=checkout)
    manager = get_plugins_manager(allow_replica=False)
    add_variant_to_checkout(fetch_checkout_info(checkout, [], manager), variant, 1)
    checkout.assigned_delivery = checkout_delivery(checkout)
    checkout.save()
    lines, _ = fetch_checkout_lines(checkout)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, fetch_checkout_info(checkout, lines, manager), lines
    )
    return checkout, total.gross


def _complete(customer) -> Callable[[Handle], None]:  # noqa: ANN001
    def complete(handle: Handle) -> None:
        # REAL PRODUCTION: complete_checkout, called as Saleor's checkoutComplete mutation calls it.
        manager = get_plugins_manager(allow_replica=False)
        checkout = Checkout.objects.get(pk=handle[0])
        lines, _ = fetch_checkout_lines(checkout)
        complete_checkout(
            manager=manager,
            checkout_info=fetch_checkout_info(checkout, lines, manager),
            lines=lines,
            payment_data={},
            store_source=False,
            user=customer,
            app=None,
            site_settings=SiteSettings.objects.get(site=Site.objects.get_current()),
            redirect_url="https://www.example.com",
        )

    return complete


def _order(checkout_pk: UUID) -> Order | None:
    return Order.objects.filter(checkout_token=str(checkout_pk)).first()


def _money_on(model: type[Payment] | type[TransactionItem], pk: object) -> str:
    """What a payment's money belongs to: its order, its checkout, or nothing at all."""
    order_id, checkout_id = model.objects.filter(pk=pk).values_list("order_id", "checkout_id").get()
    return "order" if order_id else "checkout" if checkout_id else "nothing"


def _confirmations(sent: Counter[str], order: Order) -> int:
    order_id = to_global_id_or_none(order)
    assert order_id is not None, "a saved order has a global id"
    return sent[order_id]


@pytest.fixture
def pay_with_the_payments_api(shop, confirmations: Counter[str]) -> HandoffHistory[Handle, Outcome]:  # noqa: ANN001
    """A checkout paid through Saleor's Payments API (payment plugins; here its dummy gateway)."""

    def arrange() -> Handle:
        # ARRANGE: a fresh checkout and its payment, captured by complete_checkout itself.
        checkout, total = _checkout(shop)
        payment = Payment.objects.create(
            gateway="mirumee.payments.dummy",
            checkout=checkout,
            is_active=True,
            total=total.amount,
            currency=total.currency,
            billing_email=checkout.email,
        )
        return checkout.pk, payment.pk

    def observe(handle: Handle) -> Outcome:
        # OBSERVE: the order and its history, the confirmations the customer was sent, and where the money is.
        order = _order(handle[0])
        payment = Payment.objects.get(pk=handle[1])
        return Outcome(
            order=order is not None,
            events=tuple(sorted(order.events.values_list("type", flat=True))) if order else (),
            confirmations=_confirmations(confirmations, order) if order else 0,
            charged=payment.captured_amount > 0,
            money_on=_money_on(Payment, payment.pk),
        )

    return HandoffHistory(name="complete checkout", arrange=arrange, transition=_complete(shop[2]), observe=observe)


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


def test_what_each_failure_costs_the_customer(pay_with_the_payments_api: HandoffHistory[Handle, Outcome]) -> None:
    runs = crash_histories(SALEOR, pay_with_the_payments_api)
    assert runs[0].after == PLACED, "normal operation places, records and confirms the order"
    assert {run.label: run.after for run in runs[1:]} == FINDINGS
    with pytest.raises(AssertionError, match="Work was lost or repeated"):
        assert_histories_converge(SALEOR.name, runs)


@pytest.fixture
def pay_with_transactions_and_automatic_completion(shop, transaction_item_generator, transaction_events_generator):  # noqa: ANN001, ANN201
    """A checkout paid through Saleor's Transactions API, in a channel that completes fully paid checkouts itself."""
    channel = shop[0]
    channel.automatically_complete_fully_paid_checkouts = True
    channel.automatic_completion_delay = 0
    channel.save()

    def arrange() -> Handle:
        # ARRANGE: a fresh checkout, charged by the payment app before completion, as Saleor's own tests arrange it.
        checkout, total = _checkout(shop)
        transaction = transaction_item_generator(checkout_id=checkout.pk)
        transaction_events_generator(
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

    def observe(handle: Handle) -> Paid:
        # OBSERVE: whether the charged customer has an order, and where the money is.
        transaction = TransactionItem.objects.get(pk=handle[1])
        return Paid(
            order=_order(handle[0]) is not None,
            charged=transaction.charged_value > Decimal(0),
            money_on=_money_on(TransactionItem, transaction.pk),
        )

    return HandoffHistory(name="complete checkout", arrange=arrange, transition=_complete(shop[2]), observe=observe)


def test_with_automatic_completion_a_charged_customer_always_gets_an_order(
    pay_with_transactions_and_automatic_completion: HandoffHistory[Handle, Paid],
) -> None:
    """Positive control, Saleor's own design: money held as a transaction, and a beat task that completes it."""
    assert_crash_at_every_commit_converges(SALEOR, pay_with_transactions_and_automatic_completion)
