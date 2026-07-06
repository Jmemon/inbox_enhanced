import os
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import fakeredis
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.db.models import Base, InboxThread, User
from app.task_engine import repo as task_repo
from app.workers import tasks, gmail_sync


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


def test_extend_publishes_typed_event_with_more_flag(fake_redis, session_factory, monkeypatch):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    db = session_factory()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc),
                gmail_last_history_id="100"))
    db.commit(); db.close()

    ps = fake_redis.pubsub(); ps.subscribe("user:u1"); ps.get_message(timeout=0.1)

    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = {"threads": [{"id": "gT_old"}]}
    gmail.users().threads().get().execute.return_value = {
        "id": "gT_old", "messages": [{
            "id": "m_old", "threadId": "gT_old", "internalDate": "100", "historyId": "1",
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": "Subject", "value": "old"}],
                        "body": {"data": ""}}}]}

    monkeypatch.setattr(
        "app.workers.gmail_sync.triage",
        lambda threads, buckets, trackers, current, **kw: [(None, []) for _ in threads],
    )

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        tasks.extend_inbox_history_task.apply(args=["u1", 999_999_000])

    msg = ps.get_message(timeout=1.0)
    assert msg and msg["type"] == "message"
    body = json.loads(msg["data"])
    assert body["event"] == "extend_complete"
    assert body["more"] is False  # only 1 stub returned, not 200
    assert len(body["thread_ids"]) == 1


def test_extend_enqueues_extraction_for_freshly_linked_threads(
    fake_redis, session_factory, monkeypatch,
):
    """Fix (linked-but-never-extracted): extend_inbox_history dual-writes
    task_thread_links for any pulled-in thread a tracker matches, but
    extend_inbox_history_task previously never enqueued process_task_updates
    afterward — a thread pulled in via "load more history" that matched a
    tracker would sit attached-but-unextracted until some unrelated future
    sync happened to re-touch it. extend_inbox_history_task must now enqueue
    process_task_updates with exactly the freshly-linked thread ids."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    db = session_factory()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc),
                gmail_last_history_id="100"))
    db.commit()
    task = task_repo.create_task(db, user_id="u1", name="Tracker", goal="", criteria="",
                                 state_schema=None)
    db.commit()
    task_id = task.id
    db.close()

    ps = fake_redis.pubsub(); ps.subscribe("user:u1"); ps.get_message(timeout=0.1)

    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = {"threads": [{"id": "gT_old"}]}
    gmail.users().threads().get().execute.return_value = {
        "id": "gT_old", "messages": [{
            "id": "m_old", "threadId": "gT_old", "internalDate": "100", "historyId": "1",
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": "Subject", "value": "old"}],
                        "body": {"data": ""}}}]}

    # Canned triage: this one tracker matches the pulled-in thread above threshold.
    monkeypatch.setattr(
        "app.workers.gmail_sync.triage",
        lambda threads, buckets, trackers, current, **kw: [(None, [(task_id, 95)]) for _ in threads],
    )

    enqueued: list[list] = []
    monkeypatch.setattr(
        "app.workers.task_engine_tasks.process_task_updates.apply_async",
        lambda args, countdown=0: enqueued.append(args),
    )

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        tasks.extend_inbox_history_task.apply(args=["u1", 999_999_000])

    ps.get_message(timeout=1.0)  # drain the extend_complete publish

    db2 = session_factory()
    thread = db2.execute(
        select(InboxThread).where(InboxThread.gmail_id == "gT_old")
    ).scalar_one()

    assert enqueued == [["u1", [thread.id]]]
