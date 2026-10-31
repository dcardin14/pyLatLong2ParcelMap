"""Initialize the Parcel Mapper database tables."""

from parcel_web import app
from models import db

with app.app_context():
    db.create_all()
    print("Database tables initialized successfully.")