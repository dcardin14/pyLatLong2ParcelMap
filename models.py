from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(
        db.String(255),
        unique=True,
        nullable=False,
        index=True
    )

    password_hash = db.Column(db.String(255), nullable=False)

    email_verified = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    stripe_customer_id = db.Column(
        db.String(255),
        unique=True,
        nullable=True
    )

    stripe_subscription_id = db.Column(
        db.String(255),
        unique=True,
        nullable=True
    )

    stripe_checkout_session_id = db.Column(
        db.String(255), nullable=True
    )

    stripe_checkout_attempt_id = db.Column(
    db.String(255), nullable=True
    )

    stripe_checkout_attempt_started_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )
    
    subscription_status = db.Column(
        db.String(50),
        nullable=False,
        default="inactive"
    )

    trial_ends_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
