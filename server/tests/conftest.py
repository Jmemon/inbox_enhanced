import os
# Set test env BEFORE app modules load settings.
# CELERY_TASK_ALWAYS_EAGER must be set before celery_app imports (which freezes it at first import).
# Tests are collected alphabetically, so conftest runs before individual test files.
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")
os.environ["REDIS_URL"] = "redis://localhost:6379/15"  # overridden per-test by fakeredis patches
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SESSION_SECRET"] = "test-session-secret"
# A real Fernet key (urlsafe base64-encoded 32 bytes). Constant for tests.
os.environ["ENCRYPTION_KEY"] = "zmWNn3kP4nQwiX7rT2dSvR1mY8oC0bF6jH9aLuV3eUk="
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"
os.environ["GOOGLE_REDIRECT_URI"] = "http://testserver/auth/callback"
os.environ["ENV"] = "development"
os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"  # never used; tests mock call_messages

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
