import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import URL

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: URL
    origin: str
    csrf_secret: str
    secure_cookie: bool
    training_registration: bool = False


@lru_cache
def settings() -> Settings:
    environment = os.environ.get("APP_ENV")
    production = environment == "production"
    origin = os.environ.get("APP_ORIGIN", "http://127.0.0.1:8080")
    csrf_secret = os.environ.get("APP_CSRF_SECRET", "local-demo-csrf-secret")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("APP_ORIGIN must be an origin without a trailing slash")
    if production and (parsed.scheme != "https" or len(csrf_secret) < 32):
        raise ValueError("Production requires an HTTPS origin and a 32-character CSRF secret")

    database_name = os.environ.get("APP_DB_NAME", "polytech")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    public_corpus_registration = (
        environment == "simulation" and database_name == "polytech_training_corpus"
        and parsed.scheme == "https" and os.environ.get("APP_PUBLIC_TRAINING_REGISTRATION") == "1"
    )
    return Settings(
        database_url=URL.create(
            drivername="postgresql+psycopg",
            username=os.environ.get("APP_DB_USER", "app_runtime"),
            password=os.environ.get("APP_DB_PASSWORD", "local-runtime-password"),
            host=os.environ.get("APP_DB_HOST", "db"),
            port=int(os.environ.get("APP_DB_PORT", "5432")),
            database=database_name,
        ),
        origin=origin,
        csrf_secret=csrf_secret,
        secure_cookie=parsed.scheme == "https",
        training_registration=(
            not production
            and os.environ.get("APP_TRAINING_REGISTRATION") == "1"
            and (loopback or public_corpus_registration)
        ),
    )
