"""search_repo tests exercise the non-Postgres fallback branch (tests run on
SQLite). The Postgres FTS branch shares the same contract and is verified
manually against the dev stack (see plan Task 6 step 5)."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from app.main import app
from app.db.models import Base, InboxThread, User
from app.db.session import get_db
from app.auth import sessions
from app.inbox import inbox_repo, search_repo


def _mk_user(db) -> User:
    u = User(id=uuid.uuid4().hex, email=f"{uuid.uuid4().hex}@t.co",
             created_at=datetime.now(timezone.utc))
    db.add(u)
    db.flush()
    return u


def _mk_thread(db, user, gid, subject, body, from_addr="a@b.c", date=100):
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id=gid,
                             subject=subject, bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id=gid, gmail_message_id=f"m-{gid}",
        gmail_internal_date=date, gmail_history_id="1",
        to_addr=None, from_addr=from_addr, body_preview=body[:150],
        body_text=body, label_ids=["INBOX"])


def test_search_matches_subject_body_and_sender(db):
    user = _mk_user(db)
    _mk_thread(db, user, "g1", "Stripe onsite invite", "we'd like to invite you onsite")
    _mk_thread(db, user, "g2", "Grocery list", "milk and eggs", from_addr="recruiting@stripe.com")
    _mk_thread(db, user, "g3", "Unrelated", "nothing to see")

    by_subject = search_repo.search_threads(db, user_id=user.id, q="onsite")
    assert {t.gmail_id for t in by_subject} == {"g1"}

    by_sender = search_repo.search_threads(db, user_id=user.id, q="stripe")
    assert {t.gmail_id for t in by_sender} == {"g1", "g2"}


def test_search_is_user_scoped_and_skips_archived(db):
    alice, bob = _mk_user(db), _mk_user(db)
    _mk_thread(db, alice, "ga", "topic zebra", "zebra body")
    _mk_thread(db, bob, "gb", "topic zebra", "zebra body")
    arch = db.execute(select(InboxThread).where(
        InboxThread.user_id == alice.id, InboxThread.gmail_id == "ga")).scalar_one()
    arch.is_archived = True
    # search_repo never flushes (read-only convention) — tests must flush
    # ORM mutations themselves before reading, since the `db` fixture session
    # runs with autoflush=False.
    db.flush()

    assert search_repo.search_threads(db, user_id=alice.id, q="zebra") == []
    assert len(search_repo.search_threads(db, user_id=alice.id, q="zebra",
                                          include_archived=True)) == 1
    assert len(search_repo.search_threads(db, user_id=bob.id, q="zebra")) == 1


# --- Route tests: mirror test_inbox_api.py's authed-client fixture pattern ---

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


def _seed_thread_for(TestSession, *, user_id, gmail_thread_id, internal_date, subject, body=""):
    db = TestSession()
    inbox_repo.upsert_thread(db, user_id=user_id, gmail_thread_id=gmail_thread_id,
                             subject=subject, bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id=user_id, gmail_thread_id=gmail_thread_id,
        gmail_message_id=f"m_{gmail_thread_id}",
        gmail_internal_date=internal_date, gmail_history_id=str(internal_date),
        to_addr="me@x.com", from_addr="alice@x.com", body_preview=(body or subject)[:150],
        body_text=body or subject)
    db.commit()
    db.close()


def test_search_route_returns_200_with_results_for_owned_match(authed):
    c, TestSession = authed
    _seed_thread_for(TestSession, user_id="u1", gmail_thread_id="gA",
                     internal_date=1, subject="Stripe onsite invite")
    r = c.get("/api/search?q=onsite")
    assert r.status_code == 200
    body = r.json()
    assert [t["gmail_thread_id"] for t in body["threads"]] == ["gA"]
    assert "as_of" in body
    assert body["page"] == 1


def test_search_route_422_on_missing_q(authed):
    c, TestSession = authed
    r = c.get("/api/search")
    assert r.status_code == 422


def test_search_route_empty_for_other_users_data(authed):
    c, TestSession = authed
    db = TestSession()
    db.add(User(id="other", email="other@x.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    db.close()
    # Seed matching content for a different user — u1's search must not see it.
    _seed_thread_for(TestSession, user_id="other", gmail_thread_id="gX",
                     internal_date=1, subject="onsite invite")

    r = c.get("/api/search?q=onsite")
    assert r.status_code == 200
    assert r.json()["threads"] == []
