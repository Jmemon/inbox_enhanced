import os
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import Base, User, InboxThread, InboxMessage
from app.workers import tasks


@pytest.fixture
def fake_redis(monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


@pytest.fixture
def session_factory(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)


def _seed(session_factory):
    db = session_factory()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.add(InboxThread(id="t1", user_id="u1", gmail_id="gT1", subject="bk",
                       bucket_id=None, recent_message_id=None))
    # body_text is what _score_all (Task 7) now reads via inbox_repo.load_parsed_threads
    # instead of refetching from Gmail — body_preview stays for the pre-migration fallback.
    db.add(InboxMessage(id="m1", thread_id="t1", user_id="u1", gmail_id="gM1",
                        gmail_thread_id="gT1", gmail_internal_date=1, gmail_history_id="1",
                        to_addr="me", from_addr="club@b.com", body_preview="march pick",
                        body_text="march pick — full body"))
    db.commit(); db.close()


def test_draft_preview_publishes_typed_event(fake_redis, session_factory, monkeypatch):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed(session_factory)

    ps = fake_redis.pubsub(); ps.subscribe("user:u1"); ps.get_message(timeout=0.1)

    # Task 7: draft_preview_bucket's scoring path (_score_all) reads Postgres
    # bodies and must never touch Gmail. A gmail client whose .users() raises
    # proves that — if the fake ever fires this test fails loudly instead of
    # silently swallowing a regression.
    def _raise_if_called(*a, **kw):
        raise AssertionError("get_gmail_client must not be called by the scoring path")
    gmail = MagicMock()
    gmail.users.side_effect = _raise_if_called

    # Stub the LLM scoring path so tests don't hit the real Anthropic API.
    async def _fake_call(**kw):
        return '{"score": 9, "rationale": "match", "snippet": "march pick"}'
    monkeypatch.setattr("app.workers.tasks.llm_client.call_messages", _fake_call)

    # Stub _extend_inline to no-op because only 1 thread is seeded (pool < EXTEND_THRESHOLD),
    # and extend_inbox_history (Task 13) doesn't exist yet.
    monkeypatch.setattr("app.workers.tasks._extend_inline", lambda db, *, user: None)

    with patch("app.workers.tasks.get_gmail_client", return_value=gmail):
        tasks.draft_preview_bucket.apply(args=["u1", "draft-x", "Books", "book club emails", []])

    msg = ps.get_message(timeout=1.0)
    assert msg and msg["type"] == "message"
    body = json.loads(msg["data"])
    assert body["event"] == "bucket_draft_preview" and body["draft_id"] == "draft-x"
    assert len(body["positives"]) == 1 and body["positives"][0]["score"] == 9
    gmail.users.assert_not_called()
