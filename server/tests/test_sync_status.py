"""Tests for the last-sync Redis marker and GET /api/sync/status.

Fixture wiring mirrors two existing test files rather than the brief's
placeholder names:
 - `fake_redis` — copied from test_active_users.py (monkeypatches the
   realtime redis client onto a fakeredis instance).
 - `authed` — copied from test_inbox_api.py (sqlite-backed db + TestClient
   with a session cookie already set for user "u1").
"""

from datetime import datetime, timezone
import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.db.models import Base, User
from app.db.session import get_db
from app.auth import sessions


@pytest.fixture
def fake_redis(monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


@pytest.fixture
def authed(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db

    db = TestSession()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    sid = sessions.create_session(db, user_id="u1", ttl_seconds=600)

    c = TestClient(app)
    c.cookies.set("session", sid)
    yield c, TestSession
    app.dependency_overrides.clear()
    engine.dispose()


def test_mark_then_get_roundtrip(fake_redis):
    from app.realtime import last_sync

    assert last_sync.get("u1") is None
    last_sync.mark("u1")
    got = last_sync.get("u1")
    assert isinstance(got, int) and got > 0


def test_mark_sets_a_self_expiring_ttl(fake_redis):
    """mark() must carry a TTL so a user who stops syncing entirely (account
    abandoned/deleted, Gmail access revoked) doesn't leave an unbounded
    last_sync:{uid} key in Redis forever. Refreshed on every mark(), so an
    active user's key effectively never expires in practice."""
    from app.realtime import last_sync

    last_sync.mark("u1")
    ttl = fake_redis.ttl(last_sync._key("u1"))
    assert ttl > 0
    assert ttl == last_sync.TTL_SECONDS


def test_sync_status_endpoint_shape(authed, fake_redis):
    # user seeded WITHOUT gmail_last_history_id; no mark() called.
    c, _TestSession = authed
    r = c.get("/api/sync/status")
    assert r.status_code == 200
    assert r.json() == {"last_synced_at": None, "has_cursor": False}


def test_sync_status_reflects_mark_and_cursor(authed, fake_redis):
    from app.realtime import last_sync

    c, TestSession = authed
    db = TestSession()
    user = db.get(User, "u1")
    user.gmail_last_history_id = "123"
    db.commit()
    db.close()
    last_sync.mark("u1")

    r = c.get("/api/sync/status")
    assert r.status_code == 200
    body = r.json()
    assert body["has_cursor"] is True
    assert isinstance(body["last_synced_at"], int)


def test_sync_status_unauthenticated_returns_401():
    c = TestClient(app)
    r = c.get("/api/sync/status")
    assert r.status_code == 401
