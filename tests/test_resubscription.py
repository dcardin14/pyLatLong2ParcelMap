from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from parcel_web import (
    app,
    has_open_stripe_subscription,
    checkout_session_subscription_id,
    verify_replacement_checkout,
    claim_checkout_attempt,
)
from models import User


def test_no_open_subscription_when_all_canceled():
    client = MagicMock()
    client.v1.subscriptions.list.return_value.auto_paging_iter.return_value = iter([
        NS(id="sub_old", status="canceled")
    ])

    assert not has_open_stripe_subscription(client, "cus_test")


def test_active_subscription_is_detected():
    client = MagicMock()
    client.v1.subscriptions.list.return_value.auto_paging_iter.return_value = iter([
        NS(id="sub_active", status="active")
    ])

    assert has_open_stripe_subscription(client, "cus_test")


def test_checkout_subscription_id():
    assert checkout_session_subscription_id(
        NS(subscription="sub_123")
    ) == "sub_123"

    assert checkout_session_subscription_id(
        NS(subscription=NS(id="sub_123"))
    ) == "sub_123"

    assert checkout_session_subscription_id(
        NS(subscription=None)
    ) is None


def test_replacement_checkout_requires_matching_session():
    user = User(
        id=123,
        email="test@example.com",
        stripe_customer_id="cus_test",
        stripe_subscription_id="sub_old",
        stripe_checkout_session_id="cs_new",
        stripe_checkout_attempt_id="attempt_new",
        subscription_status="canceled",
        trial_used=True,
    )

    subscription = NS(id="sub_new", status="active")

    client = MagicMock()
    client.v1.checkout.sessions.retrieve.return_value = NS(
        client_reference_id="123",
        customer="cus_test",
        status="complete",
        subscription="sub_new",
    )

    assert verify_replacement_checkout(
        client, user, subscription
    )


def test_replacement_rejects_wrong_customer():
    user = User(
        id=123,
        email="test@example.com",
        stripe_customer_id="cus_test",
        stripe_subscription_id="sub_old",
        stripe_checkout_session_id="cs_new",
        stripe_checkout_attempt_id="attempt_new",
        subscription_status="canceled",
        trial_used=True,
    )

    subscription = NS(id="sub_new", status="active")

    client = MagicMock()
    client.v1.checkout.sessions.retrieve.return_value = NS(
        client_reference_id="123",
        customer="cus_wrong",
        status="complete",
        subscription="sub_new",
    )

    assert not verify_replacement_checkout(
        client, user, subscription
    )


def test_checkout_claim_rejects_duplicate_attempt():
    with app.app_context():
        with patch("parcel_web.db.session.execute") as execute:
            with patch("parcel_web.db.session.commit"):
                execute.return_value.rowcount = 0

                assert not claim_checkout_attempt(
                    123, "attempt_new"
                )
