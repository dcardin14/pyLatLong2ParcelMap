
import os

from sqlalchemy import create_engine, text

database_url = os.environ["DATABASE_URL"]

if database_url.startswith("postgres://"):
    database_url = database_url.replace(
        "postgres://", "postgresql://", 1
    )

engine = create_engine(database_url)

with engine.begin() as connection:
    connection.execute(text("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS trial_used BOOLEAN
        NOT NULL DEFAULT FALSE
    """))

    result = connection.execute(text("""
        UPDATE users
        SET trial_used = TRUE
        WHERE stripe_subscription_id IS NOT NULL
          AND trial_used = FALSE
    """))

    print(f"Marked {result.rowcount} existing accounts as trial-used.")

print("Migration completed successfully.")
