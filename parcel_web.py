#!/usr/bin/env python3
"""Local-network photo-to-parcel Flask prototype.

Run from the pyLatLong2ParcelMap project folder:
    python parcel_web.py
Then visit http://<debian-lan-ip>:5000 from a phone on the same Wi-Fi.

Requires Flask, ExifTool, and the existing pyPhoto2Parcel.py / mapper.
Do not expose this development server to the public internet.
"""
import uuid
import contextlib
import io
import os
import secrets
import threading
import time
import stripe
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy.exc import IntegrityError
from flask import Flask, abort, render_template_string, request, send_file, url_for, Response, redirect
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from pyPhoto2Parcel import extract_coordinates, run_mapper

from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    current_user,
)
from models import db, User

BASE = Path(__file__).resolve().parent
MAX_BYTES = 25 * 1024 * 1024
app = Flask(__name__)

import os

os.environ["PATH"] = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    + os.pathsep
    + os.environ.get("PATH", "")
)

app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES

# Use a persistent secret for login sessions.
app.config["SECRET_KEY"] = os.environ.get(
    "FLASK_SECRET_KEY",
    secrets.token_hex(32)
)

# SQLite locally; PostgreSQL on Render.
database_url = os.environ.get(
    "DATABASE_URL",
    "sqlite:///parcel_users.db"
)

# Render may provide a postgres:// URL.
if database_url.startswith("postgres://"):
    database_url = database_url.replace(
        "postgres://",
        "postgresql://",
        1
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

# Customer authentication
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (ValueError, TypeError):
        return None

# The existing mapper uses fixed cache/output paths and creates a desktop image
# viewer. Run one map request at a time to avoid concurrent writes.
map_lock = threading.Lock()
results = {}  # token -> {path, details, timestamp}; local demo, memory only
RESULT_TTL = 60 * 60

# Require authentication before serving any page or processing uploads.
@app.before_request
def require_authentication():
    # These pages must be accessible without an existing account.
    public_endpoints = {
    "register", "login", "logout",
    "subscribe", "stripe_webhook", "static"
    }

    if request.endpoint in public_endpoints:
        return None
    # Allow logged-in customers with a valid subscription.
    protected_customer_endpoints = {
    "index",
    "confirm_parcel",
    "get_map",
    "get_geojson",
    }

    if request.endpoint in protected_customer_endpoints:
        if current_user.is_authenticated:
            from datetime import datetime, timezone

            if current_user.subscription_status == "active":
                return None

            if current_user.subscription_status == "trialing":
                trial_end = current_user.trial_ends_at

                if trial_end is not None:
                    # SQLite may return a datetime without timezone information.
                    if trial_end.tzinfo is None:
                        trial_end = trial_end.replace(tzinfo=timezone.utc)

                    if datetime.now(timezone.utc) < trial_end:
                        return None
    # Require customer login instead of legacy HTTP Basic Authentication.
    if not current_user.is_authenticated:
        return redirect(url_for("login"))

    # Logged-in customers without an active subscription
    # must subscribe before accessing protected features.
    return redirect(url_for("subscribe"))
#----------------------------------------------------------------------
def sync_stripe_subscription(subscription):
    """Synchronize a verified Stripe subscription with a local user."""
    from datetime import datetime, timezone

    user_id = subscription.metadata.get("user_id")

    if not user_id:
        app.logger.warning(
            "Subscription %s has no user_id metadata",
            subscription.id
        )
        return False

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    user = db.session.get(User, user_id)

    if user is None:
        app.logger.warning("Stripe user %s not found", user_id)
        return False

    customer_id = subscription.customer
    if not isinstance(customer_id, str):
        customer_id = customer_id.id

    if user.stripe_customer_id not in (None, customer_id):
        app.logger.error("Conflicting Stripe customer for user %s", user_id)
        return False

    if user.stripe_subscription_id not in (None, subscription.id):
        app.logger.error("Conflicting Stripe subscription for user %s", user_id)
        return False

    user.stripe_customer_id = customer_id
    user.stripe_subscription_id = subscription.id
    user.subscription_status = subscription.status

    user.trial_ends_at = (
        datetime.fromtimestamp(subscription.trial_end, timezone.utc)
        if subscription.trial_end else None
    )

    db.session.commit()

    app.logger.warning(
        "Synchronized subscription %s: %s",
        subscription.id,
        subscription.status
    )
    return True
#----------------------------------------------------------------------
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    if not secret:
        return Response("Webhook not configured.", status=503)

    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, signature, secret
        )
    except (ValueError, stripe.SignatureVerificationError):
        return Response("Invalid webhook signature.", status=400)

    # app.logger.info("Verified Stripe event: %s", event["type"])
    app.logger.warning("Verified Stripe event: %s", event["type"])
    if event["type"] in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        subscription_id = event["data"]["object"]["id"]

        try:
            client = stripe.StripeClient(
                os.environ["STRIPE_SECRET_KEY"]
            )
            subscription = client.v1.subscriptions.retrieve(
                subscription_id
            )
        except (KeyError, stripe.StripeError):
            app.logger.exception(
                "Unable to retrieve Stripe subscription %s",
                subscription_id
            )
            return Response("Subscription lookup failed.", status=503)

        if not sync_stripe_subscription(subscription):
            return Response("Subscription not synchronized.", status=503)

    return Response("Webhook received.", status=200)
#----------------------------------------------------------------------
def release_checkout_attempt(user_id, attempt_id):
    """Release a Checkout claim only if it still belongs to this attempt."""
    from sqlalchemy import update

    result = db.session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.stripe_checkout_attempt_id == attempt_id,
            User.stripe_checkout_session_id.is_(None),
            User.stripe_subscription_id.is_(None),
        )
        .values(
            stripe_checkout_attempt_id=None,
            stripe_checkout_attempt_started_at=None
        )
        .execution_options(synchronize_session=False)
    )

    db.session.commit()
    return result.rowcount == 1
#----------------------------------------------------------------------
def checkout_attempt_age_seconds(started_at):
    """Return the age of a Checkout attempt in seconds."""
    from datetime import datetime, timezone

    if started_at is None:
        return None

    # SQLite may return timestamps without timezone information.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    age = (
        datetime.now(timezone.utc) - started_at
    ).total_seconds()

    return max(0, age)
#----------------------------------------------------------------------
def checkout_attempt_needs_recovery(started_at):
    """Identify an old Checkout attempt for investigation."""
    age = checkout_attempt_age_seconds(started_at)

    if age is None:
        return True

    return age >= 120
#----------------------------------------------------------------------
def inspect_checkout_session(session, user_id):
    """Classify a Stripe Checkout session for a particular user."""
    from datetime import datetime, timezone

    if str(session.client_reference_id) != str(user_id):
        return "wrong_user"

    if session.subscription:
        return "subscription_exists"

    if session.status == "complete":
        return "completed"

    if session.status == "open":
        if not session.url or not session.expires_at:
            return "unusable"

        if session.expires_at <= datetime.now(timezone.utc).timestamp():
            return "expired"

        return "reusable"

    if session.status == "expired":
        return "expired"

    return "unknown"
#----------------------------------------------------------------------
def decide_checkout_recovery(sessions, user_id):
    """Choose a safe action based on existing Checkout sessions."""

    classifications = [
        (session, inspect_checkout_session(session, user_id))
        for session in sessions
    ]

    # An existing subscription takes priority over any open session.
    if any(
        result == "subscription_exists"
        for _, result in classifications
    ):
        return "verify_subscription", None

    # Completed sessions must be investigated before proceeding.
    if any(
        result == "completed"
        for _, result in classifications
    ):
        return "verify_completed", None

    # Unknown or unusable sessions require investigation.
    if any(
        result in ("unknown", "unusable", "wrong_user")
        for _, result in classifications
    ):
        return "investigate", None

    # Reuse an existing, unexpired Checkout session.
    for session, result in classifications:
        if result == "reusable":
            return "reuse", session

    # No reusable session was identified.
    return "no_reusable_session", None
#----------------------------------------------------------------------
def find_customer_checkout_sessions(stripe_client, user):
    """Find Checkout sessions associated with a known Stripe customer."""

    if not user.stripe_customer_id:
        return []

    matches = []

    try:
        sessions = stripe_client.v1.checkout.sessions.list(
            params={
                "customer": user.stripe_customer_id,
                "limit": 100,
            }
        )

        for session in sessions.auto_paging_iter():
            if str(session.client_reference_id) == str(user.id):
                matches.append(session)

    except Exception:
        app.logger.exception(
            "Unable to reconcile Checkout sessions for user %s",
            user.id
        )
        raise

    return matches
#----------------------------------------------------------------------
def customer_has_stripe_subscription(stripe_client, customer_id):
    """Check Stripe for any existing subscription, including canceled ones."""
    subscriptions = stripe_client.v1.subscriptions.list(
        params={
            "customer": customer_id,
            "status": "all",
            "limit": 1,
        }
    )
    return bool(subscriptions.data)
#----------------------------------------------------------------------
def reconcile_customer_checkout(stripe_client, user):
    """Inspect existing Checkout sessions for a known Stripe customer."""

    if not user.stripe_customer_id:
        return "customer_unknown", None

    if customer_has_stripe_subscription(
        stripe_client, user.stripe_customer_id
    ):
        return "verify_subscription", None

    sessions = find_customer_checkout_sessions(
        stripe_client, user
    )

    return decide_checkout_recovery(
        sessions, user.id
    )
#----------------------------------------------------------------------
def checkout_recovery_action(attempt_id, started_at):
    """Decide how to handle an existing Checkout attempt."""
    if not attempt_id:
        return "new"

    if not checkout_attempt_needs_recovery(started_at):
        return "wait"

    # An old attempt requires Stripe reconciliation.
    # Never automatically create a new attempt here.
    return "reconcile"

#----------------------------------------------------------------------
def clear_expired_checkout_session(user_id, session_id, attempt_id):
    """Clear a Checkout session only if its ID has not changed."""
    from sqlalchemy import update

    result = db.session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.stripe_checkout_session_id == session_id,
            User.stripe_checkout_attempt_id == attempt_id,
            User.stripe_subscription_id.is_(None),
        )
        .values(
            stripe_checkout_session_id=None,
            stripe_checkout_attempt_id=None,
            stripe_checkout_attempt_started_at=None,
        )
        .execution_options(synchronize_session=False)
    )

    db.session.commit()
    return result.rowcount == 1
#----------------------------------------------------------------------
def claim_checkout_attempt(user_id, attempt_id):
    """Atomically claim an available Checkout attempt."""
    from sqlalchemy import update
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc)
    result = db.session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.stripe_subscription_id.is_(None),
            User.stripe_checkout_attempt_id.is_(None),
        )
        .values(
            stripe_checkout_attempt_id=attempt_id,
            stripe_checkout_attempt_started_at=started_at
        )
        .execution_options(synchronize_session=False)
    )

    claimed = result.rowcount == 1
    db.session.commit()

    return claimed

#----------------------------------------------------------------------
def create_stripe_customer(stripe_client, user):
    """Create a Stripe customer associated with a Parcel Mapper user."""
    return stripe_client.v1.customers.create(
        {
            "email": user.email,
            "metadata": {"user_id": str(user.id)},
        },
        options={
            "idempotency_key": f"parcel-customer-{user.id}"
        }
    )
#----------------------------------------------------------------------
def save_stripe_customer_id(user_id, customer_id):
    """Save a Stripe customer ID without overwriting an existing one."""
    from sqlalchemy import update

    result = db.session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.stripe_customer_id.is_(None)
        )
        .values(stripe_customer_id=customer_id)
        .execution_options(synchronize_session=False)
    )

    db.session.commit()
    return result.rowcount == 1
#----------------------------------------------------------------------
def get_or_create_stripe_customer(stripe_client, user):
    """Reuse an existing Stripe customer or create one if necessary."""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer = create_stripe_customer(stripe_client, user)

    if save_stripe_customer_id(user.id, customer.id):
        return customer.id

    # Another request may have saved a customer ID first.
    db.session.refresh(user)

    if user.stripe_customer_id:
        return user.stripe_customer_id

    raise RuntimeError(
        "Unable to associate Stripe customer with user"
    )
#----------------------------------------------------------------------
@app.route("/subscribe", methods=["GET", "POST"])
def subscribe():
    if request.method == "GET":
        if not current_user.is_authenticated:
            return redirect(url_for("login"))

        if current_user.subscription_status in ("trialing", "active"):
            return redirect(url_for("index"))

        return """
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Subscribe - Texas Parcel Mapper</title>
        </head>
        <body style="font-family:Arial;max-width:450px;margin:60px auto;padding:20px;">
            <h1>Texas Parcel Mapper</h1>
            <h2>30-Day Free Trial</h2>
            <p>Then $19.99 per month. Cancel anytime.</p>
            <p>A payment card is required to begin your trial.</p>
            <form method="post" action="/subscribe">
                <button type="submit">Start My Free Trial</button>
            </form>
        </body>
        </html>
        """

    if not current_user.is_authenticated:
        return Response("Please log in first.", status=401)

    # Prevent repeat trials and duplicate Checkout attempts
    # when a subscription is already recorded locally.
    if current_user.stripe_subscription_id:
        return Response(
            "This account already has a subscription record.",
            status=409
        )

    if current_user.subscription_status in ("trialing", "active"):
        return Response(
            "You already have a subscription.",
            status=409
        )

    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    price_id = os.environ.get("STRIPE_PRICE_ID")

    if not secret_key or not price_id:
        return Response("Stripe is not configured.", status=503)

    client = stripe.StripeClient(secret_key)
    # Generate an identifier for this Checkout attempt.
    attempt_id = str(uuid.uuid4())
    # Reuse an existing, unexpired Checkout session.
    if current_user.stripe_checkout_session_id:
        try:
            existing_session = client.v1.checkout.sessions.retrieve(
                current_user.stripe_checkout_session_id
            )

            session_state = inspect_checkout_session(
                existing_session, current_user.id
            )

            if session_state == "reusable":
                return redirect(existing_session.url, code=303)
            
            # Expired sessions must be reconciled with Stripe before
            # clearing the saved session and its Checkout attempt lock.
            # Refresh current_user after any successful database reset.
            if session_state == "expired":
                if not current_user.stripe_customer_id:
                    return Response(
                        "Unable to verify the Stripe customer.",
                        status=409
                    )

                try:
                    recovery_action, recovered_session = reconcile_customer_checkout(
                        client, current_user
                    )
                except Exception:
                    app.logger.exception(
                        "Expired Checkout reconciliation failed for user %s",
                        current_user.id
                    )
                    return Response(
                        "Unable to verify previous Checkout. Please try later.",
                        status=503
                    )

                if recovery_action == "reuse":
                    return redirect(recovered_session.url, code=303)

                return Response(
                    "Checkout recovery requires verification before retrying.",
                    status=409
                )

            return Response(
                "Existing Checkout session requires verification. "
                "Please contact support.",
                status=409
            )

        except stripe.StripeError:
            app.logger.exception(
                "Unable to retrieve existing Checkout session"
            )
            return Response(
                "Unable to verify existing checkout.",
                status=502
            )
        # Check whether a previous Checkout attempt needs attention.
    action = checkout_recovery_action(
        current_user.stripe_checkout_attempt_id,
        current_user.stripe_checkout_attempt_started_at
    )

    if action == "wait":
        return Response(
            "Checkout is already being started. "
            "Please wait a moment and try again.",
            status=409
        )

    if action == "reconcile":
        try:
            recovery_action, existing_session = (
                reconcile_customer_checkout(client, current_user)
            )
        except stripe.StripeError:
            app.logger.exception(
                "Stripe Checkout reconciliation failed for user %s",
                current_user.id
            )
            return Response(
                "Unable to verify earlier Checkout. Please try later.",
                status=503
            )
        except Exception:
            app.logger.exception(
                "Unexpected Checkout reconciliation error for user %s",
                current_user.id
            )
            return Response(
                "Unable to verify earlier Checkout. Please try later.",
                status=503
            )
        if recovery_action == "reuse":
            return redirect(existing_session.url, code=303)

        return Response(
            "An earlier Checkout attempt needs verification. "
            "Please contact support.",
            status=409
        )
    # Only one request may claim the right to create Checkout.
    if not claim_checkout_attempt(current_user.id, attempt_id):
        return Response(
            "Checkout is already being started for this account. "
            "Please wait and try again.",
            status=409
        )
    try:
        customer_id = get_or_create_stripe_customer(
            client, current_user
        )

        session = client.v1.checkout.sessions.create({
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "customer": customer_id,
            "client_reference_id": str(current_user.id),
            "payment_method_collection": "always",
            "subscription_data": {
                "trial_period_days": 30,
                "metadata": {"user_id": str(current_user.id)},
            },
            "success_url": url_for("login", _external=True)
                + "?checkout=success",
            "cancel_url": url_for("login", _external=True)
                + "?checkout=cancelled",
        }, options={
            "idempotency_key": f"checkout-{attempt_id}"
        })
    except stripe.StripeError:
        app.logger.exception("Stripe Checkout creation failed")
        return Response("Unable to start checkout.", status=502)
    except Exception:
        db.session.rollback()
        app.logger.exception(
            "Unexpected customer or Checkout creation failure for user %s",
            current_user.id
        )
        return Response(
            "Unable to start checkout. Please try again later.",
            status=503
        )
    # Remember this Checkout session so it can be reused.
    try:
        current_user.stripe_checkout_session_id = session.id
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Unable to save Checkout session")
        return Response(
            "Unable to save checkout information.",
            status=500
        )
    return redirect(session.url, code=303)
#----------------------------------------------------------------------

@app.route("/logout", methods=["POST"])
def logout():
    if not current_user.is_authenticated:
        return Response("Not logged in.", status=401)

    logout_user()
    return Response("Logged out successfully.", status=200)
#----------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()

        if user is None or not user.check_password(password):
            return Response(
                "Invalid email address or password.",
                status=401,
            )

        login_user(user)

        return redirect(url_for("subscribe"))

    return """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Login - Texas Parcel Mapper</title>
    </head>
    <body style="font-family:Arial;max-width:450px;margin:60px auto;padding:20px;">
        <h1>Customer Login</h1>
        <form method="post" action="/login">
            <label>Email address</label>
            <input type="email" name="email" required
                   autocomplete="email"
                   style="display:block;width:100%;margin:10px 0 20px;">

            <label>Password</label>
            <input type="password" name="password" required
                   autocomplete="current-password"
                   style="display:block;width:100%;margin:10px 0 20px;">

            <button type="submit">Sign In</button>
        </form>
        <p><a href="/register">Create an account</a></p>
    </body>
    </html>
    """
#----------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or len(email) > 255 or "@" not in email:
            return Response("Invalid email address.", status=400)

        if len(password) < 12 or len(password) > 1024:
            return Response(
                "Password must contain between 12 and 1024 characters.",
                status=400,
            )

        if User.query.filter_by(email=email).first():
            return Response("Account already exists.", status=409)

        user = User(email=email)
        user.set_password(password)

        try:
            db.session.add(user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return Response("Account already exists.", status=409)

        return Response(
            "Account created successfully. Login will be available soon.",
            status=201,
        )
    return """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Create Account - Texas Parcel Mapper</title>
    </head>
    <body style="font-family:Arial;max-width:450px;margin:60px auto;padding:20px;">
        <h1>Create Your Account</h1>
        <p>Start your 30-day free trial.</p>

        <form method="post" action="/register">
            <label>Email address</label>
            <input type="email" name="email" required
                   autocomplete="email" style="display:block;width:100%;margin:10px 0 20px;">

            <label>Password</label>
            <input type="password" name="password" required minlength="12"
                   autocomplete="new-password"
                   style="display:block;width:100%;margin:10px 0 20px;">

            <button type="submit">Create Account</button>
        </form>
    </body>
    </html>
    """

#----------------------------------------------------------------------
PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Texas Photo-to-Parcel</title>
<!-- Leaflet interactive mapping library -->
<link
  rel="stylesheet"
  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@turf/turf@6.5.0/turf.min.js"></script>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#192a3b;background:#f2f5f8}
main{max-width:700px;margin:2rem auto;padding:1.5rem;background:white;border-radius:16px;box-shadow:0 3px 20px #0001}
h1{font-size:1.55rem}p{line-height:1.5}input{display:block;width:100%;margin:1rem 0;font-size:1rem}
button,.download{display:inline-block;border:0;border-radius:9px;background:#135e9a;color:white;padding:.85rem 1.15rem;font-weight:650;text-decoration:none;font-size:1rem;cursor:pointer}
.error{color:#a02020;background:#fff2f2;padding:1rem;border-radius:8px}.muted{color:#596a77;font-size:.92rem}
img{width:100%;height:auto;border-radius:9px;border:1px solid #ddd}
#interactive_map {
    width: 100%;
    height: 450px;
    border: 1px solid #ddd;
    border-radius: 9px;
    margin: 1rem 0;
}

#interactive_map {
    position: relative;
}

#center_dot {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 14px;
    height: 14px;
    background: #0066ff;
    border: 3px solid white;
    border-radius: 50%;
    box-shadow: 0 0 0 2px #0066ff, 0 2px 8px #0005;
    transform: translate(-50%, -50%);
    z-index: 1000;
    pointer-events: none;
}

pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:1rem;border-radius:8px}
.spinner{display:inline-block;width:18px;height:18px;border:3px solid #cbd5e1;border-top-color:#135e9a;border-radius:50%;animation:spin .8s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
#processing_status{margin-top:1rem;padding:1rem;background:#eef6fc;border-radius:9px}
#processing_status[hidden]{display:none}
button:disabled{opacity:.6;cursor:wait}
</style></head><body><main>
<h1>Texas Photo-to-Parcel</h1>
<p>Upload an <strong>original GPS-tagged photograph</strong>. Your existing Python program will identify the parcel and generate an ownership map.</p>
<form id="parcel_form" method="post" enctype="multipart/form-data">
<label for="photo"><strong>Select original photograph</strong></label>
<input id="photo" type="file" name="photo" accept="image/jpeg,image/heic,image/heif,image/tiff">
<input type="hidden" id="phone_lat" name="phone_lat">
<input type="hidden" id="phone_lon" name="phone_lon">
<input type="hidden" id="phone_accuracy" name="phone_accuracy">
<button type="button" id="location_button" onclick="usePhoneLocation()">Use My Current Location</button>
<p id="location_status" class="muted" role="status"></p>
<button type="submit" id="submit_button">Identify parcel</button>
<div id="processing_status" role="status" aria-live="polite" hidden>
  <p><span class="spinner"></span> <strong>Identifying parcel...</strong></p>
  <p id="processing_timer" class="muted">Elapsed time: 0 seconds</p>
  <p class="muted">Locating parcel records and generating your map. This may take a minute.</p>
</div>
</form>
<script>
document.getElementById("parcel_form").addEventListener("submit", function() {
    const button = document.getElementById("submit_button");
    const status = document.getElementById("processing_status");
    const timer = document.getElementById("processing_timer");

    status.hidden = false;
    button.disabled = true;
    button.textContent = "Processing...";

    const started = Date.now();
    setInterval(function() {
        const seconds = Math.floor((Date.now() - started) / 1000);
        timer.textContent = "Elapsed time: " + seconds + " seconds";
    }, 1000);
});

function usePhoneLocation() {
    const status = document.getElementById("location_status");
    const button = document.getElementById("location_button");

    if (!navigator.geolocation) {
        status.textContent = "Your browser does not support location services.";
        return;
    }

    button.disabled = true;
    status.textContent = "Requesting your phone's location...";

    navigator.geolocation.getCurrentPosition(
        function(position) {
            document.getElementById("phone_lat").value = position.coords.latitude;
            document.getElementById("phone_lon").value = position.coords.longitude;
            document.getElementById("phone_accuracy").value = position.coords.accuracy;
            status.textContent = "Phone GPS acquired. You can now identify the parcel.";
            button.disabled = false;
        },
        function(error) {
            document.getElementById("phone_lat").value = "";
            document.getElementById("phone_lon").value = "";
            document.getElementById("phone_accuracy").value = "";
            status.textContent = "Unable to get location: " + error.message;
            button.disabled = false;
        },
        {enableHighAccuracy: true, timeout: 15000, maximumAge: 0}
    );
}
</script>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
{% if result %}
{% if result.match_type == "exact" %}
<p style="background:#e8f5e9;padding:1rem;border-radius:9px">
<strong>Exact parcel intersection</strong><br>
Your GPS point falls inside or on the boundary of this parcel.
</p>
{% elif result.match_type == "nearby" %}
<p style="background:#fff3cd;padding:1rem;border-radius:9px">
<strong>Nearby parcel — verify this result</strong><br>
Your GPS point falls outside this parcel.
The nearest parcel is {{ result.match_distance }} meters away.
This may not be the correct property.
</p>
{% else %}
<p class="muted">Parcel match method could not be determined.</p>
{% endif %}
<h2>GPS Location Acquired</h2>
<p class="muted">The GPS location is an initial estimate. Use the interactive map below to select and confirm the correct parcel.</p>
<p><strong>{{ result.gps_source }}:</strong> {{ result.lat }}, {{ result.lon }}</p>
{% if result.accuracy is not none %}
<p><strong>Reported GPS accuracy:</strong> ±{{ result.accuracy }} feet</p>
{% endif %}

<h2>Interactive Parcel Map</h2>
<p class="muted">Drag the map beneath the blue dot until it is positioned over the correct parcel. Review the ownership information, then click Confirm This Parcel.</p>
<div id="interactive_map"></div>
<p id="adjusted_coordinates" class="muted"></p>

<div id="selected_parcel_details">
    <p class="muted">Move the map to select a parcel.</p>
</div>

<button type="button" id="confirm_parcel" disabled>
    Confirm This Parcel
</button>

<button type="button" id="change_parcel" hidden>
    Change Selection
</button>

<p id="selection_status" class="muted"></p>

<script>
(function() {
    const lat = {{ result.lat | tojson }};
    const lon = {{ result.lon | tojson }};
    const geojsonUrl = {{ url_for('get_geojson', token=result.token) | tojson }};

    const map = L.map("interactive_map").setView(
        [Number(lat), Number(lon)], 17
    );

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 19
    }).addTo(map);

    // Original GPS location, preserved for reference.
    const originalGps = L.latLng(Number(lat), Number(lon));

    // Fixed blue dot centered over the map.
    const centerDot = document.createElement("div");
    centerDot.id = "center_dot";
    centerDot.title = "Move the map to position this dot over a parcel";

    document.getElementById("interactive_map").appendChild(centerDot);

    // Track the geographic coordinates underneath the fixed dot.
    function updateCenterPosition() {
        const center = map.getCenter();

        document.getElementById("adjusted_coordinates").textContent =
            "Selected location: " +
            center.lat.toFixed(7) + ", " +
            center.lng.toFixed(7);
    }

    map.on("moveend", updateCenterPosition);
    updateCenterPosition();
    // ---------------------------------------------------------
        const detailsBox = document.getElementById("selected_parcel_details");
    const confirmButton = document.getElementById("confirm_parcel");
    const changeButton = document.getElementById("change_parcel");
    const selectionStatus = document.getElementById("selection_status");

    let selectedFeature = null;
    let selectedLayer = null;
    let selectionLocked = false;

    function updateSelectedParcel(data, parcels) {
        if (selectionLocked) return;

        const center = map.getCenter();
        const point = turf.point([center.lng, center.lat]);

        let matchedFeature = null;

        for (const feature of data.features) {
            if (!feature.geometry) continue;

            if (turf.booleanPointInPolygon(point, feature)) {
                matchedFeature = feature;
                break;
            }
        }

        if (selectedLayer) {
            parcels.resetStyle(selectedLayer);
            selectedLayer = null;
        }

        selectedFeature = matchedFeature;

        if (!matchedFeature) {
            detailsBox.textContent =
                "No parcel found beneath the blue dot. Move the map to another property.";
            confirmButton.disabled = true;
            selectionStatus.textContent = "";
            return;
        }

        const p = matchedFeature.properties || {};

        detailsBox.innerHTML =
            "<strong>Owner:</strong> " + escapeHtml(p.OWNER_NAME) +
            "<br><strong>Property ID:</strong> " + escapeHtml(p.PROP_ID) +
            "<br><strong>Geo ID:</strong> " + escapeHtml(p.GEO_ID) +
            "<br><strong>Address:</strong> " + escapeHtml(p.SITUS_ADDR);

        parcels.eachLayer(function(layer) {
            if (layer.feature === matchedFeature) {
                selectedLayer = layer;
                layer.setStyle({
                    color: "red",
                    weight: 3,
                    fillColor: "#fff5a5",
                    fillOpacity: 0.45
                });
            }
        });

        confirmButton.disabled = false;
        selectionStatus.textContent = "Move the map to change the selected parcel.";
    }

    fetch(geojsonUrl)
        .then(response => {
            if (!response.ok) throw new Error("GeoJSON request failed");
            return response.json();
        })
        .then(data => {
            const parcels = L.geoJSON(data, {
                style: {
                    color: "#555",
                    weight: 1,
                    fillColor: "#3388ff",
                    fillOpacity: 0.08
                },
                onEachFeature: function(feature, layer) {
                    const p = feature.properties || {};

                    layer.bindPopup(
                        "<strong>Owner:</strong> " + escapeHtml(p.OWNER_NAME) +
                        "<br><strong>Property ID:</strong> " + escapeHtml(p.PROP_ID) +
                        "<br><strong>Geo ID:</strong> " + escapeHtml(p.GEO_ID) +
                        "<br><strong>Address:</strong> " + escapeHtml(p.SITUS_ADDR)
                    );
                }
            }).addTo(map);

            // Keep the original GPS position centered initially.
            map.setView(originalGps, 17);

            map.on("moveend", function() {
                updateSelectedParcel(data, parcels);
            });

            updateSelectedParcel(data, parcels);

             confirmButton.addEventListener("click", async function() {
                if (!selectedFeature || selectionLocked) return;

                const center = map.getCenter();
                const propId = selectedFeature.properties.PROP_ID;

                confirmButton.disabled = true;
                selectionStatus.textContent =
                    "Generating final parcel report and PNG...";

                try {
                    const response = await fetch(
                        "{{ url_for('confirm_parcel', token=result.token) }}",
                        {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json"
                            },
                            body: JSON.stringify({
                                lat: center.lat,
                                lon: center.lng,
                                prop_id: String(propId)
                            })
                        }
                    );

                    const result = await response.json();

                    if (!response.ok || !result.success) {
                        throw new Error(
                            result.error || "Parcel confirmation failed."
                        );
                    }

                    selectionLocked = true;

                    map.dragging.disable();
                    map.touchZoom.disable();
                    map.doubleClickZoom.disable();
                    map.scrollWheelZoom.disable();
                    map.boxZoom.disable();
                    map.keyboard.disable();

                    confirmButton.hidden = true;
                    changeButton.hidden = false;

                    const finalDetails =
                        document.getElementById("final_parcel_details");

                    finalDetails.textContent = result.details;

                    const finalMap =
                        document.getElementById("final_parcel_map");

                    const finalDownload =
                        document.getElementById("final_map_download");

                    const refreshedUrl =
                        result.map_url + "?v=" + Date.now();

                    finalMap.src = refreshedUrl;
                    finalDownload.href = refreshedUrl;

                    document.getElementById(
                        "final_parcel_result"
                    ).hidden = false;

                    selectionStatus.textContent =
                        "Parcel confirmed. Final report and PNG generated.";

                } catch (error) {
                    console.error("Confirmation error:", error);
                    selectionStatus.textContent =
                        "Confirmation failed: " + error.message;
                } finally {
                    if (!selectionLocked) {
                        confirmButton.disabled = false;
                    }
                }
            });

            changeButton.addEventListener("click", function() {
                selectionLocked = false;

                map.dragging.enable();
                map.touchZoom.enable();
                map.doubleClickZoom.enable();
                map.scrollWheelZoom.enable();
                map.boxZoom.enable();
                map.keyboard.enable();

                confirmButton.hidden = false;
                changeButton.hidden = true;

                updateSelectedParcel(data, parcels);
            });
        })
        .catch(error => {
            console.error("Interactive map error:", error);
            detailsBox.textContent =
                "Unable to load parcel boundaries. Please try again.";
        });
    //------------------------------------------------
    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }
})();
</script>

<div id="final_parcel_result" hidden>
    <h2>Confirmed Parcel Report</h2>

    <pre id="final_parcel_details"></pre>

    <h2>Final Parcel Map</h2>

    <a
        id="final_map_download"
        class="download"
        href="#"
        download="parcel_map.png"
    >
        Save parcel map (PNG)
    </a>

    <p>
        <img
            id="final_parcel_map"
            alt="Confirmed parcel ownership map"
        >
    </p>
</div>
{% endif %}
<p class="muted">Local-network prototype. GPS identifies the camera position, which may not be the photographed property. Appraisal/GIS ownership is preliminary, not a title opinion. Do not publish this server to the internet.</p>
</main></body></html>"""


def purge_old_results():
    now = time.time()
    for token, item in list(results.items()):
        if now - item["timestamp"] > RESULT_TTL:
            results.pop(token, None)


def parcel_details_from_output(output: str, png: Path) -> str:
    """Extract the fields already printed by the existing mapping script."""
    fields = ("OWNER_NAME", "PROP_ID", "GEO_ID", "LEGAL_AREA", "LEGAL_DESC", "SITUS_ADDR")
    found = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in fields:
            found[key.strip()] = value.strip()
    if found:
        return "\n".join(f"{field}: {found[field]}" for field in fields if field in found)
    return f"Map: {png.name}"


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    return render_template_string(PAGE, error="File exceeds 25 MB. Please choose a smaller original photo.", result=None), 413


@app.route("/", methods=["GET", "POST"])
def index():
    purge_old_results()
    if request.method == "GET":
        return render_template_string(PAGE, error=None, result=None)

    uploaded = request.files.get("photo")
    has_photo = bool(uploaded and uploaded.filename)

    phone_lat = request.form.get("phone_lat", "").strip()
    phone_lon = request.form.get("phone_lon", "").strip()
    phone_accuracy = request.form.get("phone_accuracy", "").strip()

    if not has_photo and not (phone_lat and phone_lon):
        return render_template_string(
            PAGE,
            error="Select a photograph or tap 'Use My Current Location'.",
            result=None,
        ), 400

    suffix = ""
    if has_photo:
        suffix = Path(secure_filename(uploaded.filename)).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff"}:
            return render_template_string(
                PAGE,
                error="Please upload a JPEG, HEIC, HEIF, or TIFF photograph.",
                result=None,
            ), 400

    try:
        with TemporaryDirectory(prefix="parcel_photo_") as tmp:
            gps_source = "Phone GPS"

            if has_photo:
                photo_path = Path(tmp) / ("upload" + suffix)
                uploaded.save(photo_path)

                try:
                    lat, lon = extract_coordinates(photo_path)
                    gps_source = "Photo GPS"
                except ValueError:
                    if not (phone_lat and phone_lon):
                        raise ValueError(
                            "This photograph has no GPS coordinates. "
                            "Tap 'Use My Current Location' and try again."
                        )

            if gps_source == "Phone GPS":
                try:
                    lat = float(phone_lat)
                    lon = float(phone_lon)
                except ValueError:
                    raise ValueError("Invalid phone GPS coordinates.")

                import math
                if not (
                    math.isfinite(lat)
                    and math.isfinite(lon)
                    and -90 <= lat <= 90
                    and -180 <= lon <= 180
                ):
                    raise ValueError("Invalid phone GPS coordinates.")

            if not map_lock.acquire(blocking=False):
                return render_template_string(PAGE, error="Another map is being generated. Please try again shortly.", result=None), 429
            try:
                log = io.StringIO()
                with contextlib.redirect_stdout(log):
                    map_path, geojson_path = run_mapper(
                        lat, lon, include_geojson=True
                    )
                output = log.getvalue()
                app.logger.info("Map generation completed: %s", map_path.name)
                # The existing mapper overwrites maps for the same parcel. Make a
                # separate snapshot per request so links remain stable.
                token = secrets.token_urlsafe(24)
                snapshot_dir = BASE / "web_results"
                snapshot_dir.mkdir(exist_ok=True)
                snapshot = snapshot_dir / f"{token}.png"
                import shutil
                shutil.copy2(map_path, snapshot)

                geojson_snapshot = snapshot_dir / f"{token}.geojson"
                shutil.copy2(geojson_path, geojson_snapshot)

                results[token] = {
                    "path": snapshot,
                    "geojson_path": geojson_snapshot,
                    "timestamp": time.time(),
                }
            finally:
                map_lock.release()
        import re
        match_type = "unknown"
        match_distance = None

        if "Exact parcel match found." in output:
            match_type = "exact"
        else:
            match = re.search(
                r"Nearest parcel distance: ([0-9.]+) meters",
                output
            )
            if match:
                match_type = "nearby"
                match_distance = match.group(1)

        accuracy_display = None
        if gps_source == "Phone GPS" and phone_accuracy:
            try:
                import math
                accuracy_value = float(phone_accuracy)
                if math.isfinite(accuracy_value) and accuracy_value >= 0:
                    accuracy_display = f"{accuracy_value * 3.28084:.1f}"
            except ValueError:
                pass

        result = {"accuracy": accuracy_display, "match_type": match_type, "match_distance": match_distance, "lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "gps_source": gps_source, "details": parcel_details_from_output(output, map_path), "token": token}
        return render_template_string(PAGE, error=None, result=result)
    except (RuntimeError, OSError, ValueError) as exc:
        app.logger.exception("Photo processing failed")
        return render_template_string(PAGE, error=str(exc), result=None), 400

@app.route("/confirm/<token>", methods=["POST"])
def confirm_parcel(token):
    purge_old_results()

    item = results.get(token)
    if item is None:
        abort(404)

    data = request.get_json(silent=True) or {}

    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
        prop_id = str(data["prop_id"])

        import math
        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
            and prop_id.strip()
        ):
            raise ValueError("Invalid confirmed parcel selection.")

        if not map_lock.acquire(blocking=False):
            return {"error": "Another map is being generated. Please try again."}, 429

        try:
            log = io.StringIO()

            with contextlib.redirect_stdout(log):
                map_path = run_mapper(
                    lat,
                    lon,
                    exact_only=True,
                    prop_id=prop_id,
                )

            output = log.getvalue()

            import shutil
            shutil.copy2(map_path, item["path"])

            item["details"] = parcel_details_from_output(
                output, map_path
            )
            item["confirmed"] = True

        finally:
            map_lock.release()

        return {
            "success": True,
            "details": item["details"],
            "map_url": f"/map/{token}",
        }

    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        app.logger.exception("Parcel confirmation failed")
        return {"error": str(exc)}, 400
@app.route("/map/<token>")
def get_map(token):
    purge_old_results()
    item = results.get(token)
    if item is None or not item["path"].is_file():
        abort(404)
    return send_file(item["path"], mimetype="image/png", as_attachment=False, download_name="parcel_map.png")

@app.route("/geojson/<token>")
def get_geojson(token):
    purge_old_results()

    item = results.get(token)

    if item is None:
        abort(404)

    geojson_path = item.get("geojson_path")

    if geojson_path is None or not geojson_path.is_file():
        abort(404)

    return send_file(
        geojson_path,
        mimetype="application/geo+json",
        as_attachment=False
    )

if __name__ == "__main__":
    print("Photo-to-parcel web app running on port 5000")
    print("Open http://<your-debian-computer-LAN-IP>:5000 on your phone")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
