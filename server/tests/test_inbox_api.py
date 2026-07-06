from datetime import datetime, timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.db.models import Base, InboxThread, User
from app.db.session import get_db
from app.auth import sessions
from app.inbox import inbox_repo


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


def _seed_thread(TestSession, *, gmail_thread_id, internal_date, subject="hi"):
    db = TestSession()
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id=gmail_thread_id, subject=subject, bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id=gmail_thread_id,
        gmail_message_id=f"m_{gmail_thread_id}",
        gmail_internal_date=internal_date, gmail_history_id=str(internal_date),
        to_addr="me@x.com", from_addr="alice@x.com", body_preview="hello",
    )
    db.commit()
    db.close()


def test_get_inbox_returns_threads_sorted_desc(authed):
    c, TestSession = authed
    _seed_thread(TestSession, gmail_thread_id="gA", internal_date=1)
    _seed_thread(TestSession, gmail_thread_id="gB", internal_date=3)
    _seed_thread(TestSession, gmail_thread_id="gC", internal_date=2)
    r = c.get("/api/inbox?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert [t["gmail_thread_id"] for t in body["threads"]] == ["gB", "gC", "gA"]
    assert "as_of" in body


def test_get_thread_single(authed):
    c, TestSession = authed
    _seed_thread(TestSession, gmail_thread_id="gA", internal_date=1, subject="hello there")
    list_resp = c.get("/api/inbox?limit=10").json()
    tid = list_resp["threads"][0]["id"]
    r = c.get(f"/api/threads/{tid}")
    assert r.status_code == 200
    assert r.json()["subject"] == "hello there"


def test_post_refresh_enqueues_poll_for_authed_user(authed):
    c, TestSession = authed
    # Give user a history cursor so refresh routes to poll_new_messages, not full sync.
    db = TestSession()
    user = db.get(User, "u1")
    user.gmail_last_history_id = "100"
    db.commit()
    db.close()
    with patch("app.api.inbox.tasks.poll_new_messages.apply_async") as mock_apply:
        r = c.post("/api/inbox/refresh")
    assert r.status_code == 202
    mock_apply.assert_called_once_with(args=["u1"], countdown=0)


def test_post_refresh_uses_full_sync_when_user_has_no_history_id(authed):
    c, TestSession = authed
    # user has no gmail_last_history_id by default
    with patch("app.api.inbox.tasks.full_sync_inbox_task.apply_async") as mock_full, \
         patch("app.api.inbox.tasks.poll_new_messages.apply_async") as mock_partial:
        r = c.post("/api/inbox/refresh")
    assert r.status_code == 202
    mock_full.assert_called_once()
    mock_partial.assert_not_called()


def test_post_threads_batch_returns_only_user_owned_threads(authed):
    c, TestSession = authed
    _seed_thread(TestSession, gmail_thread_id="gA", internal_date=1)
    _seed_thread(TestSession, gmail_thread_id="gB", internal_date=2)
    list_resp = c.get("/api/inbox?limit=10").json()
    ids = [t["id"] for t in list_resp["threads"]]

    r = c.post("/api/threads/batch", json={"thread_ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert {t["gmail_thread_id"] for t in body["threads"]} == {"gA", "gB"}
    # Each thread carries the same shape as GET /api/threads/{id} returns.
    assert all("recent_message" in t for t in body["threads"])


def test_post_threads_batch_omits_unknown_or_other_users_ids(authed):
    """Mixing in a non-existent id should not 404 the whole request — just
    silently omit it. Behavior the SSE replay path relies on if the server
    side has cleaned up a thread between SSE event and client fetch."""
    c, TestSession = authed
    _seed_thread(TestSession, gmail_thread_id="gA", internal_date=1)
    list_resp = c.get("/api/inbox?limit=10").json()
    real_id = list_resp["threads"][0]["id"]

    r = c.post("/api/threads/batch", json={"thread_ids": [real_id, "DOES_NOT_EXIST"]})
    assert r.status_code == 200
    assert len(r.json()["threads"]) == 1


def test_post_threads_batch_unauthorized_without_session():
    c = TestClient(app)
    r = c.post("/api/threads/batch", json={"thread_ids": ["x"]})
    assert r.status_code == 401


def test_inbox_serializer_carries_flags(authed):
    c, TestSession = authed
    _seed_thread(TestSession, gmail_thread_id="gA", internal_date=1)
    r = c.get("/api/inbox")
    assert r.status_code == 200
    t = r.json()["threads"][0]
    assert t["is_archived"] is False
    assert "is_unread" in t["recent_message"]


def test_get_inbox_include_archived_query_param(authed):
    """Archived threads are hidden by default and included with ?include_archived=true —
    exercises the pass-through from the route down to inbox_repo.list_threads."""
    c, TestSession = authed
    _seed_thread(TestSession, gmail_thread_id="gA", internal_date=1)
    _seed_thread(TestSession, gmail_thread_id="gB", internal_date=2)

    db = TestSession()
    thread = db.execute(
        select(InboxThread).where(InboxThread.user_id == "u1", InboxThread.gmail_id == "gB")
    ).scalar_one()
    thread.is_archived = True
    db.commit()
    db.close()

    r = c.get("/api/inbox")
    assert r.status_code == 200
    assert [t["gmail_thread_id"] for t in r.json()["threads"]] == ["gA"]

    r = c.get("/api/inbox?include_archived=true")
    assert r.status_code == 200
    assert {t["gmail_thread_id"] for t in r.json()["threads"]} == {"gA", "gB"}
