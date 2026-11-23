from uuid import uuid4

import pytest

from app import auth
from app.models import User
from app.settings import settings


@pytest.mark.parametrize("environment,database,origin,role,enabled", [
    ("simulation", "polytech_training_corpus", "http://127.0.0.1:18087", "trainee", True),
    ("simulation", "polytech_training_corpus", "https://corpus.example", "trainee", True),
    ("simulation", "polytech_training_corpus", "http://127.0.0.1:18087", "operator", False),
    ("simulation", "polytech", "http://127.0.0.1:18087", "trainee", False),
    ("development", "polytech_training_corpus", "http://127.0.0.1:18087", "trainee", False),
    ("production", "polytech", "https://operations.example", "operator", False),
])
def test_session_enables_companion_only_for_corpus_trainees(monkeypatch, environment, database, origin, role, enabled):
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("APP_DB_NAME", database)
    monkeypatch.setenv("APP_ORIGIN", origin)
    monkeypatch.setenv("APP_CSRF_SECRET", "test-corpus-companion-csrf-secret-1234")
    monkeypatch.setenv("APP_TRAINING_REGISTRATION", "0")
    monkeypatch.setattr(auth, "settings", settings.__wrapped__)
    user = User(id=uuid4(), username="synthetic", display_name="Synthetic", role=role)

    payload = auth.session_payload(user, "synthetic-session")

    assert payload["roles"] == [role]
    assert ("corpus_training" in payload) is enabled
    if enabled:
        assert payload["corpus_training"] is True
