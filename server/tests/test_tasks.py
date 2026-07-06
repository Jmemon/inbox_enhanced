import os
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import Base, InboxMessage, InboxThread, User
from app.inbox import inbox_repo
from app.realtime import last_sync
from app.workers import tasks, gmail_sync


@pytest.fixture
def fake_redis(monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return SessionLocal


def _seed_user(session_factory, *, history_id="100"):
    db = session_factory()
    db.add(User(
        id="u1", email="x@y.com",
        created_at=datetime.now(timezone.utc),
        gmail_last_history_id=history_id,
    ))
    db.commit()
    db.close()


def _drained_pubsub(fake_redis, channel: str):
    ps = fake_redis.pubsub()
    ps.subscribe(channel)
    ps.get_message(timeout=0.1)  # drain the subscribe-confirmation
    return ps


def test_poll_new_messages_publishes_when_history_returns_records(
    fake_redis, session_factory, monkeypatch,
):
    """Happy path: history.list returns records → partial_sync called with them
    → ids published. history.list MUST be called by the task itself, not by
    partial_sync_inbox."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory)
    ps = _drained_pubsub(fake_redis, "user:u1")

    fake_records = [{"id": "200", "messagesAdded": [{"message": {"id": "gM1", "threadId": "gT1"}}]}]

    gmail = MagicMock()
    with patch("app.workers.tasks.get_gmail_client", return_value=gmail), \
         patch("app.workers.tasks.gmail_sync.fetch_history_records",
               return_value=(fake_records, "200")) as mock_fetch, \
         patch("app.workers.tasks.gmail_sync.partial_sync_inbox",
               return_value=["gT1"]) as mock_partial:
        tasks.poll_new_messages.apply(args=["u1"])

    mock_fetch.assert_called_once()
    # The records fetched at task level must be passed through, not re-fetched.
    _, kwargs = mock_partial.call_args
    assert kwargs["history_records"] == fake_records
    assert kwargs["new_history_id"] == "200"

    msg = ps.get_message(timeout=1.0)
    assert msg and msg["type"] == "message"
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT1"]

    # last_sync.mark(user_id) must fire on the partial-sync-complete exit path.
    assert last_sync.get("u1") is not None


def test_poll_new_messages_silent_when_history_returns_no_records(
    fake_redis, session_factory, monkeypatch,
):
    """No new history records → no publish, no partial_sync call."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory)
    ps = _drained_pubsub(fake_redis, "user:u1")

    gmail = MagicMock()
    with patch("app.workers.tasks.get_gmail_client", return_value=gmail), \
         patch("app.workers.tasks.gmail_sync.fetch_history_records",
               return_value=([], "100")), \
         patch("app.workers.tasks.gmail_sync.partial_sync_inbox") as mock_partial:
        tasks.poll_new_messages.apply(args=["u1"])

    mock_partial.assert_not_called()
    assert ps.get_message(timeout=0.2) is None  # nothing published

    # a successful check IS a sync, even with nothing new — mark() must still fire.
    assert last_sync.get("u1") is not None


def test_poll_new_messages_falls_back_to_full_sync_on_404(
    fake_redis, session_factory, monkeypatch,
):
    """When history.list raises HistoryGoneError (gmail 404), the task must
    invoke full_sync_inbox and publish the resulting ids."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory)
    ps = _drained_pubsub(fake_redis, "user:u1")

    gmail = MagicMock()
    with patch("app.workers.tasks.get_gmail_client", return_value=gmail), \
         patch("app.workers.tasks.gmail_sync.fetch_history_records",
               side_effect=gmail_sync.HistoryGoneError()), \
         patch("app.workers.tasks.gmail_sync.full_sync_inbox",
               return_value=["gT_new"]) as mock_full, \
         patch("app.workers.tasks.gmail_sync.partial_sync_inbox") as mock_partial:
        tasks.poll_new_messages.apply(args=["u1"])

    mock_full.assert_called_once()
    mock_partial.assert_not_called()

    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT_new"]

    # 404-recovery full-sync exit path must also mark last_sync.
    assert last_sync.get("u1") is not None


def test_poll_new_messages_does_full_sync_when_user_has_no_history_id(
    fake_redis, session_factory, monkeypatch,
):
    """First-time poll (no cursor): skip history.list entirely, go full_sync."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory, history_id=None)
    ps = _drained_pubsub(fake_redis, "user:u1")

    with patch("app.workers.tasks.gmail_sync.fetch_history_records") as mock_fetch, \
         patch("app.workers.tasks.gmail_sync.full_sync_inbox",
               return_value=["gT_a"]) as mock_full:
        tasks.poll_new_messages.apply(args=["u1"])

    mock_fetch.assert_not_called()
    mock_full.assert_called_once()
    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT_a"]

    # no-cursor full-sync exit path must also mark last_sync.
    assert last_sync.get("u1") is not None


def test_full_sync_inbox_task_marks_last_sync(
    fake_redis, session_factory, monkeypatch,
):
    """full_sync_inbox_task's success exit is one of the 6 last_sync.mark
    call sites — pin that it fires after publishing."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory)
    ps = _drained_pubsub(fake_redis, "user:u1")

    with patch("app.workers.tasks.gmail_sync.full_sync_inbox",
               return_value=["gT_full"]) as mock_full:
        tasks.full_sync_inbox_task.apply(args=["u1"])

    mock_full.assert_called_once()
    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT_full"]
    assert last_sync.get("u1") is not None


def test_reclassify_user_inbox_marks_last_sync(
    fake_redis, session_factory, monkeypatch,
):
    """reclassify_user_inbox's success exit is one of the 6 last_sync.mark
    call sites. Uses a cursor-less user with an empty inbox so
    _inline_reload takes the full-sync branch and _reclassify_all
    short-circuits on zero threads, without needing to stub the LLM
    classify path."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory, history_id=None)
    ps = _drained_pubsub(fake_redis, "user:u1")

    with patch("app.workers.tasks.gmail_sync.full_sync_inbox",
               return_value=["gT_reload"]) as mock_full:
        tasks.reclassify_user_inbox.apply(args=["u1"])

    mock_full.assert_called_once()
    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT_reload"]
    assert last_sync.get("u1") is not None


def test_enqueue_polls_purges_and_fans_out(fake_redis, monkeypatch):
    fake_redis.zadd("active_users", {"u1": 99999999999, "u2": 99999999999})
    enqueued: list[str] = []
    monkeypatch.setattr("app.workers.tasks.poll_new_messages.apply_async",
                        lambda args, countdown=0: enqueued.append(args[0]))

    tasks.enqueue_polls.apply()

    assert sorted(enqueued) == ["u1", "u2"]


def test_reclassify_all_reads_postgres_no_gmail(session_factory):
    """Task 7: _reclassify_all rebuilds ParsedThreads from stored Postgres
    rows via inbox_repo.load_parsed_threads — it must never refetch Gmail.
    A get_gmail_client stub that raises if called proves the path is
    Gmail-free; body_text is seeded straight into Postgres via
    upsert_message, mirroring what sync would have persisted."""
    db = session_factory()
    user = User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc))
    db.add(user)
    db.commit()

    thread = inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT1",
                                      subject="hi", bucket_id="old-bucket")
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="gT1", gmail_message_id="gM1",
        gmail_internal_date=1, gmail_history_id="1",
        to_addr=None, from_addr="a@b.com", body_preview="prev",
        body_text="full body text",
    )
    db.commit()

    def _raise_if_called(*a, **kw):
        raise AssertionError("get_gmail_client must not be called by _reclassify_all")

    with patch("app.workers.tasks.get_gmail_client", side_effect=_raise_if_called), \
         patch("app.workers.tasks.bucket_repo.list_active", return_value=[]) as mock_buckets, \
         patch("app.workers.tasks.classify", return_value=["new-bucket"]) as mock_classify:
        changed = tasks._reclassify_all(db, user=user)

    mock_buckets.assert_called_once()
    mock_classify.assert_called_once()
    assert changed == [thread.id]
    assert thread.bucket_id == "new-bucket"


def test_read_candidates_sorts_by_last_activity_and_skips_archived(session_factory):
    """_read_candidates must sort by InboxThread.last_activity_at desc (the
    denormalized pointer list_threads already uses) and exclude is_archived
    threads — not the old recent_message_id-joined InboxMessage.gmail_internal_date
    sort, which ignored both. t_old carries a LATER gmail_internal_date on its
    joined message than t_new does, despite an OLDER last_activity_at — proving
    the sort key really is the thread pointer, not the message date."""
    db = session_factory()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.commit()

    db.add_all([
        InboxThread(id="t_old", user_id="u1", gmail_id="g_old", subject="old",
                   bucket_id=None, recent_message_id="m_old", last_activity_at=200,
                   is_archived=False),
        InboxMessage(id="m_old", thread_id="t_old", user_id="u1", gmail_id="g_m_old",
                    gmail_thread_id="g_old", gmail_internal_date=999, gmail_history_id="1",
                    to_addr="me", from_addr="old@x.com", body_preview="old preview"),
        InboxThread(id="t_new", user_id="u1", gmail_id="g_new", subject="new",
                   bucket_id=None, recent_message_id="m_new", last_activity_at=500,
                   is_archived=False),
        InboxMessage(id="m_new", thread_id="t_new", user_id="u1", gmail_id="g_m_new",
                    gmail_thread_id="g_new", gmail_internal_date=1, gmail_history_id="1",
                    to_addr="me", from_addr="new@x.com", body_preview="new preview"),
        InboxThread(id="t_arch", user_id="u1", gmail_id="g_arch", subject="archived",
                   bucket_id=None, recent_message_id="m_arch", last_activity_at=999999,
                   is_archived=True),
        InboxMessage(id="m_arch", thread_id="t_arch", user_id="u1", gmail_id="g_m_arch",
                    gmail_thread_id="g_arch", gmail_internal_date=1, gmail_history_id="1",
                    to_addr="me", from_addr="arch@x.com", body_preview="archived preview"),
    ])
    db.commit()

    out = tasks._read_candidates(db, user_id="u1", exclude=set(), limit=100)

    ids = [c["thread_id"] for c in out]
    assert "t_arch" not in ids, "archived threads must be excluded from candidates"
    assert ids == ["t_new", "t_old"], \
        "must order by InboxThread.last_activity_at desc, not the joined message's date"
