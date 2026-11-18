import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import password_hasher
from app.db import engine
from app.models import User
from app.settings import settings


def bootstrap_operator() -> None:
    settings()
    username = os.environ.get("APP_AUTH_USERNAME", "demo").strip().casefold()
    password = os.environ.get("APP_BOOTSTRAP_PASSWORD", "local-demo-password")
    minimum = 16 if os.environ.get("APP_ENV") == "production" else 8
    if not 1 <= len(username) <= 64 or not minimum <= len(password) <= 256:
        raise ValueError("Bootstrap username or password length is invalid")

    with Session(engine()) as db:
        if db.scalar(select(User.id).where(User.username == username)) is None:
            db.add(
                User(
                    username=username,
                    display_name="운영자",
                    password_hash=password_hasher.hash(password),
                    is_active=True,
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()


if __name__ == "__main__":
    bootstrap_operator()
