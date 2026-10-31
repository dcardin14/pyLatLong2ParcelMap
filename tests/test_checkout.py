from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from parcel_web import customer_has_stripe_subscription

from parcel_web import inspect_checkout_session, decide_checkout_recovery


def make_session(user_id="123", expires_in=1800):
    return SimpleNamespace(
        client_reference_id=user_id,
        subscription=None,
        status="open",
        url="https://checkout.stripe.com/test",
        expires_at=int(datetime.now(timezone.utc).timestamp()) + expires_in,
    )


def test_valid_checkout_session():
    session = make_session()
    assert inspect_checkout_session(session, 123) == "reusable"


def test_expired_checkout_session():
    session = make_session(expires_in=-60)
    assert inspect_checkout_session(session, 123) == "expired"


def test_checkout_session_wrong_user():
    session = make_session(user_id="456")
    assert inspect_checkout_session(session, 123) == "wrong_user"


def test_checkout_with_existing_subscription():
    session = make_session()
    session.subscription = "sub_existing_123"

    assert inspect_checkout_session(session, 123) == "subscription_exists"


def test_completed_checkout_session():
    session = make_session()
    session.status = "complete"

    assert inspect_checkout_session(session, 123) == "completed"


def test_recovery_reuses_valid_session():
    session = make_session()

    action, recovered_session = decide_checkout_recovery(
        [session], 123
    )

    assert action == "reuse"
    assert recovered_session is session

def test_recovery_prioritizes_existing_subscription():
    open_session = make_session()

    subscribed_session = make_session()
    subscribed_session.subscription = "sub_existing_123"

    action, recovered_session = decide_checkout_recovery(
        [open_session, subscribed_session], 123
    )

    assert action == "verify_subscription"
    assert recovered_session is None

from parcel_web import app


def test_subscribe_requires_login():
    with app.test_client() as client:
        response = client.post("/subscribe")

    assert response.status_code == 401
    assert b"Please log in first" in response.data

from unittest.mock import patch
from models import User


def test_existing_subscriber_cannot_subscribe_again():
    user = User(
        id=123,
        email="subscriber@example.com",
        subscription_status="active",
        stripe_subscription_id="sub_existing_123",
    )

    with app.test_client() as client:
        with patch(
            "parcel_web.login_manager._user_callback",
            return_value=user,
        ):
            with client.session_transaction() as session:
                session["_user_id"] = "123"
                session["_fresh"] = True

            response = client.post("/subscribe")

    assert response.status_code == 409

from types import SimpleNamespace


def test_subscribe_reuses_existing_checkout():
    user = User(
        id=123,
        email="customer@example.com",
        subscription_status="inactive",
        stripe_checkout_session_id="cs_test_existing",
    )

    existing = make_session()
    existing.id = "cs_test_existing"

    with app.test_client() as client:
        with (
            patch("parcel_web.login_manager._user_callback", return_value=user),
            patch("parcel_web.stripe.StripeClient") as mock_stripe,
            patch.dict("parcel_web.os.environ", {
                "STRIPE_SECRET_KEY": "sk_test_mock",
                "STRIPE_PRICE_ID": "price_test_mock",
            }),
        ):
            mock_stripe.return_value.v1.checkout.sessions.retrieve.return_value = existing

            with client.session_transaction() as session:
                session["_user_id"] = "123"
                session["_fresh"] = True

            response = client.post("/subscribe")

            assert response.status_code == 303
            assert response.headers["Location"] == existing.url
            mock_stripe.return_value.v1.checkout.sessions.create.assert_not_called()

def test_subscribe_rejects_expired_checkout():
    user = User(
        id=123,
        email="customer@example.com",
        subscription_status="inactive",
        stripe_checkout_session_id="cs_test_expired",
    )

    expired = make_session(expires_in=-60)
    expired.id = "cs_test_expired"

    with app.test_client() as client:
        with (
            patch("parcel_web.login_manager._user_callback", return_value=user),
            patch("parcel_web.stripe.StripeClient") as mock_stripe,
            patch.dict("parcel_web.os.environ", {
                "STRIPE_SECRET_KEY": "sk_test_mock",
                "STRIPE_PRICE_ID": "price_test_mock",
            }),
        ):
            mock_stripe.return_value.v1.checkout.sessions.retrieve.return_value = expired

            with client.session_transaction() as session:
                session["_user_id"] = "123"
                session["_fresh"] = True

            response = client.post("/subscribe")

            assert response.status_code == 409
            mock_stripe.return_value.v1.checkout.sessions.create.assert_not_called()

def test_subscribe_rejects_another_users_checkout():
    user = User(
        id=123,
        email="customer@example.com",
        subscription_status="inactive",
        stripe_checkout_session_id="cs_test_other_user",
    )

    other_session = make_session(user_id="456")
    other_session.id = "cs_test_other_user"

    with app.test_client() as client:
        with (
            patch("parcel_web.login_manager._user_callback", return_value=user),
            patch("parcel_web.stripe.StripeClient") as mock_stripe,
            patch.dict("parcel_web.os.environ", {
                "STRIPE_SECRET_KEY": "sk_test_mock",
                "STRIPE_PRICE_ID": "price_test_mock",
            }),
        ):
            mock_stripe.return_value.v1.checkout.sessions.retrieve.return_value = other_session

            with client.session_transaction() as session:
                session["_user_id"] = "123"
                session["_fresh"] = True

            response = client.post("/subscribe")

            assert response.status_code == 409
            mock_stripe.return_value.v1.checkout.sessions.create.assert_not_called()

def test_recovery_allows_retry_when_all_sessions_expired():
    expired_session = make_session(expires_in=-60)

    action, recovered_session = decide_checkout_recovery(
        [expired_session], 123
    )

    assert action == "no_reusable_session"
    assert recovered_session is None

def test_customer_with_existing_stripe_subscription():
    mock_stripe = MagicMock()

    mock_stripe.v1.subscriptions.list.return_value.data = [
        SimpleNamespace(id="sub_existing_123")
    ]

    result = customer_has_stripe_subscription(
        mock_stripe, "cus_test_123"
    )

    assert result is True

    mock_stripe.v1.subscriptions.list.assert_called_once_with(
        params={
            "customer": "cus_test_123",
            "status": "all",
            "limit": 1,
        }
    )

def test_customer_without_stripe_subscription():
    mock_stripe = MagicMock()

    mock_stripe.v1.subscriptions.list.return_value.data = []

    result = customer_has_stripe_subscription(
        mock_stripe, "cus_test_123"
    )

    assert result is False

from parcel_web import reconcile_customer_checkout


def test_recovery_blocks_existing_stripe_subscription():
    user = User(
        id=123,
        email="customer@example.com",
        stripe_customer_id="cus_test_123",
    )

    mock_stripe = MagicMock()
    mock_stripe.v1.subscriptions.list.return_value.data = [
        SimpleNamespace(id="sub_existing_123")
    ]

    action, session = reconcile_customer_checkout(
        mock_stripe, user
    )

    assert action == "verify_subscription"
    assert session is None

    mock_stripe.v1.checkout.sessions.list.assert_not_called()

def test_recovery_with_no_subscription_or_checkout():
    user = User(
        id=123,
        email="customer@example.com",
        stripe_customer_id="cus_test_123",
    )

    mock_stripe = MagicMock()
    mock_stripe.v1.subscriptions.list.return_value.data = []

    mock_stripe.v1.checkout.sessions.list.return_value.auto_paging_iter.return_value = iter([])

    action, session = reconcile_customer_checkout(
        mock_stripe, user
    )

    assert action == "no_reusable_session"
    assert session is None

    mock_stripe.v1.subscriptions.list.assert_called_once()
    mock_stripe.v1.checkout.sessions.list.assert_called_once()

from parcel_web import clear_expired_checkout_session


def test_clear_expired_checkout_session():
    with app.app_context():
        from parcel_web import db

        db.session.remove()

        with patch("parcel_web.db.session.execute") as mock_execute, \
             patch("parcel_web.db.session.commit") as mock_commit:

            mock_execute.return_value.rowcount = 1

            result = clear_expired_checkout_session(
                123, "cs_test_expired", "attempt_old"
            )
            

            assert result is True
            mock_execute.assert_called_once()
            mock_commit.assert_called_once()

            mock_execute.reset_mock()
            mock_execute.return_value.rowcount = 0

            result = clear_expired_checkout_session(
                123, "cs_test_expired", "attempt_old"
            )

            assert result is False

from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session
from models import User


def test_expired_checkout_compare_and_set():
    engine = create_engine("sqlite://")

    User.__table__.create(engine)

    try:
        with Session(engine) as session:
            session.add(User(
                id=123,
                email="test@example.com",
                password_hash="test_hash",
                stripe_checkout_session_id="cs_original",
            ))
            session.commit()

            def clear_if_matches(checkout_id):
                result = session.execute(
                    update(User)
                    .where(
                        User.id == 123,
                        User.stripe_checkout_session_id == checkout_id,
                        User.stripe_subscription_id.is_(None),
                    )
                    .values(stripe_checkout_session_id=None)
                )
                session.commit()
                return result.rowcount

            assert clear_if_matches("cs_wrong") == 0
            assert session.get(User, 123).stripe_checkout_session_id == "cs_original"

            assert clear_if_matches("cs_original") == 1
            assert session.get(User, 123).stripe_checkout_session_id is None
    finally:
        engine.dispose()

def test_expired_checkout_reset_clears_all_three_fields():
    from sqlalchemy import update
    from sqlalchemy.orm import Session
    from datetime import datetime, timezone

    engine = create_engine("sqlite://")
    User.__table__.create(engine)

    try:
        with Session(engine) as session:
            session.add(User(
                id=123,
                email="reset@example.com",
                password_hash="test_hash",
                stripe_checkout_session_id="cs_expired",
                stripe_checkout_attempt_id="attempt_old",
                stripe_checkout_attempt_started_at=datetime.now(timezone.utc),
            ))
            session.commit()

            result = session.execute(
                update(User)
                .where(
                    User.id == 123,
                    User.stripe_checkout_session_id == "cs_expired",
                    User.stripe_subscription_id.is_(None),
                )
                .values(
                    stripe_checkout_session_id=None,
                    stripe_checkout_attempt_id=None,
                    stripe_checkout_attempt_started_at=None,
                )
            )
            session.commit()
            session.expire_all()

            user = session.get(User, 123)

            assert result.rowcount == 1
            assert user.stripe_checkout_session_id is None
            assert user.stripe_checkout_attempt_id is None
            assert user.stripe_checkout_attempt_started_at is None
    finally:
        engine.dispose()


def test_clear_expired_checkout_updates_all_fields():
    from unittest.mock import patch
    from sqlalchemy.dialects import sqlite

    with app.app_context():
        with (
            patch("parcel_web.db.session.execute") as mock_execute,
            patch("parcel_web.db.session.commit"),
        ):
            mock_execute.return_value.rowcount = 1

            result = clear_expired_checkout_session(
                123, "cs_expired", "attempt_old"
            )

            statement = mock_execute.call_args.args[0]

            sql = str(statement.compile(
                dialect=sqlite.dialect(),
                compile_kwargs={"literal_binds": True},
            ))

            assert result is True
            assert "stripe_checkout_session_id=NULL" in sql
            assert "stripe_checkout_attempt_id=NULL" in sql
            assert "stripe_checkout_attempt_started_at=NULL" in sql
            assert "stripe_checkout_attempt_id = 'attempt_old'" in sql


def test_expired_checkout_does_not_reset_existing_subscription():
    user = User(
        id=123,
        email="customer@example.com",
        stripe_customer_id="cus_test_123",
        stripe_checkout_session_id="cs_expired",
        stripe_checkout_attempt_id="attempt_old",
        subscription_status="inactive",
    )

    expired = make_session(expires_in=-60)
    expired.id = "cs_expired"

    with app.test_client() as client:
        with (
            patch("parcel_web.login_manager._user_callback", return_value=user),
            patch("parcel_web.stripe.StripeClient") as mock_stripe,
            patch("parcel_web.clear_expired_checkout_session") as mock_clear,
            patch.dict("parcel_web.os.environ", {
                "STRIPE_SECRET_KEY": "sk_test_mock",
                "STRIPE_PRICE_ID": "price_test_mock",
            }),
        ):
            mock_stripe.return_value.v1.checkout.sessions.retrieve.return_value = expired
            mock_stripe.return_value.v1.subscriptions.list.return_value.data = [
                SimpleNamespace(id="sub_existing_123")
            ]

            with client.session_transaction() as session:
                session["_user_id"] = "123"
                session["_fresh"] = True

            response = client.post("/subscribe")

            assert response.status_code == 409
            mock_stripe.return_value.v1.subscriptions.list.assert_called_once()
            mock_clear.assert_not_called()
            mock_stripe.return_value.v1.checkout.sessions.create.assert_not_called()

def test_expired_checkout_recovery_requires_no_subscription():
    user = User(
        id=123,
        email="customer@example.com",
        stripe_customer_id="cus_test_123",
        stripe_checkout_session_id="cs_expired",
        stripe_checkout_attempt_id="attempt_old",
        subscription_status="inactive",
    )

    expired = make_session(expires_in=-60)
    expired.id = "cs_expired"

    with app.test_client() as client:
        with (
            patch("parcel_web.login_manager._user_callback", return_value=user),
            patch("parcel_web.stripe.StripeClient") as mock_stripe,
            patch("parcel_web.clear_expired_checkout_session") as mock_clear,
            patch.dict("parcel_web.os.environ", {
                "STRIPE_SECRET_KEY": "sk_test_mock",
                "STRIPE_PRICE_ID": "price_test_mock",
            }),
        ):
            mock_stripe.return_value.v1.checkout.sessions.retrieve.return_value = expired
            mock_stripe.return_value.v1.subscriptions.list.return_value.data = []

            with client.session_transaction() as session:
                session["_user_id"] = "123"
                session["_fresh"] = True

            response = client.post("/subscribe")

            assert response.status_code == 409
            mock_stripe.return_value.v1.subscriptions.list.assert_called_once()
            mock_clear.assert_not_called()


def test_expired_checkout_reuses_another_valid_session():
    user = User(
        id=123,
        email="customer@example.com",
        stripe_customer_id="cus_test_123",
        stripe_checkout_session_id="cs_expired",
        stripe_checkout_attempt_id="attempt_old",
        subscription_status="inactive",
    )

    expired = make_session(expires_in=-60)
    expired.id = "cs_expired"

    reusable = make_session(expires_in=3600)
    reusable.id = "cs_reusable"
    reusable.url = "https://checkout.stripe.com/c/pay/test-reusable"

    with app.test_client() as client:
        with (
            patch("parcel_web.login_manager._user_callback", return_value=user),
            patch("parcel_web.stripe.StripeClient") as mock_stripe,
            patch("parcel_web.clear_expired_checkout_session") as mock_clear,
            patch.dict("parcel_web.os.environ", {
                "STRIPE_SECRET_KEY": "sk_test_mock",
                "STRIPE_PRICE_ID": "price_test_mock",
            }),
        ):
            stripe_client = mock_stripe.return_value
            stripe_client.v1.checkout.sessions.retrieve.return_value = expired
            stripe_client.v1.subscriptions.list.return_value.data = []
            stripe_client.v1.checkout.sessions.list.return_value.auto_paging_iter.return_value = iter(
                [expired, reusable]
            )

            with client.session_transaction() as session:
                session["_user_id"] = "123"
                session["_fresh"] = True

            response = client.post("/subscribe")

            assert response.status_code == 303
            assert response.location == reusable.url
            mock_clear.assert_not_called()
            stripe_client.v1.checkout.sessions.create.assert_not_called()