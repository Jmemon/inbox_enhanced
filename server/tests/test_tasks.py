import os
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

import json
import zlib
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import Base, InboxMessage, InboxThread, User
from app.inbox import inbox_repo
from app.realtime import last_sync
from app.task_engine import repo as task_repo
from app.workers import tasks, gmail_sync, task_engine_tasks


@pytest.fixture
def fake_redis(monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


@pytest.fixture(autouse=True)
def stub_enqueue(monkeypatch):
    """Task 7: poll_new_messages/full_sync_inbox_task now enqueue
    task_engine_tasks.process_task_updates after publishing thread ids
    (reclassify_user_inbox did too, before Phase 4 Task 2 deleted it in favor
    of backfill_task's kind='bucket' branch — see test_task_engine_tasks.py).
    These tests exercise the sync tasks themselves, not extraction, and don't
    patch task_engine_tasks.SessionLocal — so left unstubbed, eager celery
    would run process_task_updates for real against the production
    (schema-less in these tests) DB. Stub the enqueue call and record its
    args so the handful of tests that care can assert the hook fired with the
    right (user_id, ids)."""
    enqueued: list[list] = []
    monkeypatch.setattr(
        "app.workers.task_engine_tasks.process_task_updates.apply_async",
        lambda args, countdown=0: enqueued.append(args),
    )
    return enqueued


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
    fake_redis, session_factory, monkeypatch, stub_enqueue,
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
               return_value=(["gT1"], ["gT1"])) as mock_partial:
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
    # Task 7: the enqueue hook must fire with this user's touched ids.
    assert stub_enqueue == [["u1", ["gT1"]]]


def test_poll_new_messages_flag_only_touch_publishes_but_skips_extraction(
    fake_redis, session_factory, monkeypatch, stub_enqueue,
):
    """Fix (cost leak): a flag-only partial sync (unread flip, archive/
    unarchive, soft-delete) has all_ids non-empty but content_ids empty —
    partial_sync_inbox returns (all_ids, content_ids). poll_new_messages must
    still publish all_ids (so the client sees the flip) but must NOT enqueue
    process_task_updates, since no content actually changed for a tracker to
    extract from."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory)
    ps = _drained_pubsub(fake_redis, "user:u1")

    fake_records = [{"id": "200", "labelsAdded": [{"message": {"id": "gM1", "threadId": "gT1"},
                                                    "labelIds": ["UNREAD"]}]}]

    gmail = MagicMock()
    with patch("app.workers.tasks.get_gmail_client", return_value=gmail), \
         patch("app.workers.tasks.gmail_sync.fetch_history_records",
               return_value=(fake_records, "200")), \
         patch("app.workers.tasks.gmail_sync.partial_sync_inbox",
               return_value=(["gT1"], [])) as mock_partial:
        tasks.poll_new_messages.apply(args=["u1"])

    mock_partial.assert_called_once()
    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT1"]  # all_ids still published

    # content_ids empty → the enqueue hook must stay silent.
    assert stub_enqueue == []


def test_poll_new_messages_enqueues_content_ids_only_not_all_ids(
    fake_redis, session_factory, monkeypatch, stub_enqueue,
):
    """Fix (cost leak): when a partial sync batch mixes a messagesAdded thread
    (content) with a flag-only-touched thread (no content), all_ids (used for
    publish) must include both, but process_task_updates must be enqueued
    with EXACTLY the content ids — never the flag-only id."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory)
    ps = _drained_pubsub(fake_redis, "user:u1")

    fake_records = [{"id": "200", "messagesAdded": [{"message": {"id": "gM1", "threadId": "gT1"}}]}]

    gmail = MagicMock()
    with patch("app.workers.tasks.get_gmail_client", return_value=gmail), \
         patch("app.workers.tasks.gmail_sync.fetch_history_records",
               return_value=(fake_records, "200")), \
         patch("app.workers.tasks.gmail_sync.partial_sync_inbox",
               return_value=(["gT1", "gT_flag_only"], ["gT1"])) as mock_partial:
        tasks.poll_new_messages.apply(args=["u1"])

    mock_partial.assert_called_once()
    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert sorted(body["thread_ids"]) == ["gT1", "gT_flag_only"]  # all_ids published

    # enqueue must carry exactly the content ids, not the flag-only extra.
    assert stub_enqueue == [["u1", ["gT1"]]]


def test_poll_new_messages_silent_when_history_returns_no_records(
    fake_redis, session_factory, monkeypatch, stub_enqueue,
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
    # No touched ids → the `if ids:` guard must keep the enqueue hook silent.
    assert stub_enqueue == []


def test_poll_new_messages_falls_back_to_full_sync_on_404(
    fake_redis, session_factory, monkeypatch, stub_enqueue,
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
               return_value=(["gT_new"], ["gT_new"])) as mock_full, \
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
    assert stub_enqueue == [["u1", ["gT_new"]]]


def test_poll_new_messages_does_full_sync_when_user_has_no_history_id(
    fake_redis, session_factory, monkeypatch, stub_enqueue,
):
    """First-time poll (no cursor): skip history.list entirely, go full_sync."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory, history_id=None)
    ps = _drained_pubsub(fake_redis, "user:u1")

    with patch("app.workers.tasks.gmail_sync.fetch_history_records") as mock_fetch, \
         patch("app.workers.tasks.gmail_sync.full_sync_inbox",
               return_value=(["gT_a"], ["gT_a"])) as mock_full:
        tasks.poll_new_messages.apply(args=["u1"])

    mock_fetch.assert_not_called()
    mock_full.assert_called_once()
    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT_a"]

    # no-cursor full-sync exit path must also mark last_sync.
    assert last_sync.get("u1") is not None
    assert stub_enqueue == [["u1", ["gT_a"]]]


def test_full_sync_inbox_task_marks_last_sync(
    fake_redis, session_factory, monkeypatch, stub_enqueue,
):
    """full_sync_inbox_task's success exit is one of the 6 last_sync.mark
    call sites — pin that it fires after publishing."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    _seed_user(session_factory)
    ps = _drained_pubsub(fake_redis, "user:u1")

    with patch("app.workers.tasks.gmail_sync.full_sync_inbox",
               return_value=(["gT_full"], ["gT_full"])) as mock_full:
        tasks.full_sync_inbox_task.apply(args=["u1"])

    mock_full.assert_called_once()
    msg = ps.get_message(timeout=1.0)
    body = json.loads(msg["data"])
    assert body["event"] == "threads_updated"
    assert body["thread_ids"] == ["gT_full"]
    assert last_sync.get("u1") is not None
    assert stub_enqueue == [["u1", ["gT_full"]]]


def test_enqueue_polls_purges_and_fans_out(fake_redis, monkeypatch):
    fake_redis.zadd("active_users", {"u1": 99999999999, "u2": 99999999999})
    enqueued: list[str] = []
    monkeypatch.setattr("app.workers.tasks.poll_new_messages.apply_async",
                        lambda args, countdown=0: enqueued.append(args[0]))

    tasks.enqueue_polls.apply()

    assert sorted(enqueued) == ["u1", "u2"]


def test_enqueue_tracker_owner_polls_enqueues_offline_owner_with_deterministic_countdown(
    fake_redis, session_factory, monkeypatch,
):
    """A user with an active, schema-bearing tracker but no open tab (not in
    active_users) must get a poll enqueued, sharded across the hour via a
    deterministic crc32-derived countdown so tracker owners advance even
    without a live SSE connection."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    db = session_factory()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    task_repo.create_task(db, user_id="u1", name="Tracker", goal="", criteria="",
                          state_schema={"stage": None})
    db.commit()
    db.close()

    enqueued: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.tasks.poll_new_messages.apply_async",
        lambda args, countdown=0: enqueued.append((tuple(args), countdown)),
    )

    tasks.enqueue_tracker_owner_polls.apply()

    expected_countdown = zlib.crc32("u1".encode()) % 3600
    assert enqueued == [(("u1",), expected_countdown)]


def test_enqueue_tracker_owner_polls_skips_user_already_active(
    fake_redis, session_factory, monkeypatch,
):
    """A tracker owner who's also in active_users is already covered by the
    30s enqueue_polls fan-out — must not be double-enqueued here."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    db = session_factory()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    task_repo.create_task(db, user_id="u1", name="Tracker", goal="", criteria="",
                          state_schema={"stage": None})
    db.commit()
    db.close()
    fake_redis.zadd("active_users", {"u1": 99999999999})

    enqueued: list = []
    monkeypatch.setattr(
        "app.workers.tasks.poll_new_messages.apply_async",
        lambda args, countdown=0: enqueued.append(args),
    )

    tasks.enqueue_tracker_owner_polls.apply()

    assert enqueued == []


def test_enqueue_tracker_owner_polls_excludes_paused_deleted_and_schemaless(
    fake_redis, session_factory, monkeypatch,
):
    """Paused trackers, soft-deleted trackers, and classify-only tasks (no
    state_schema yet) don't qualify — only active, non-deleted, schema-
    bearing trackers get an hourly poll."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    db = session_factory()
    for uid in ["u_paused", "u_deleted", "u_noschema"]:
        db.add(User(id=uid, email=f"{uid}@b.com", created_at=datetime.now(timezone.utc)))
    db.commit()

    t_paused = task_repo.create_task(db, user_id="u_paused", name="T", goal="", criteria="",
                                     state_schema={"stage": None})
    t_paused.status = "paused"
    t_deleted = task_repo.create_task(db, user_id="u_deleted", name="T", goal="", criteria="",
                                      state_schema={"stage": None})
    t_deleted.is_deleted = True
    task_repo.create_task(db, user_id="u_noschema", name="T", goal="", criteria="",
                          state_schema=None)
    db.commit()
    db.close()

    enqueued: list = []
    monkeypatch.setattr(
        "app.workers.tasks.poll_new_messages.apply_async",
        lambda args, countdown=0: enqueued.append(args),
    )

    tasks.enqueue_tracker_owner_polls.apply()

    assert enqueued == []


def test_enqueue_tracker_owner_polls_purges_expired_entries_before_reading_active(
    fake_redis, session_factory, monkeypatch,
):
    """A tracker owner with an EXPIRED active_users entry (from an unclean SSE
    disconnect) must still get enqueued by the hourly path. Without purge_expired
    being called, the stale entry would remain in the registry and mask this
    offline owner from the hourly poll indefinitely."""
    import time
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    db = session_factory()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    task_repo.create_task(db, user_id="u1", name="Tracker", goal="", criteria="",
                          state_schema={"stage": None})
    db.commit()
    db.close()

    # Simulate a stale SSE entry: zadd with a score in the past (expired).
    fake_redis.zadd("active_users", {"u1": time.time() - 1000})

    enqueued: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.tasks.poll_new_messages.apply_async",
        lambda args, countdown=0: enqueued.append((tuple(args), countdown)),
    )

    tasks.enqueue_tracker_owner_polls.apply()

    # The expired entry should have been purged, so u1 is NOT in the "active" set
    # and should be enqueued by the hourly path.
    expected_countdown = zlib.crc32("u1".encode()) % 3600
    assert enqueued == [(("u1",), expected_countdown)]


def test_enqueue_tracker_owner_polls_no_trackers_no_enqueue(
    fake_redis, session_factory, monkeypatch,
):
    """No tracker rows at all — cheap early return, nothing enqueued."""
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)

    enqueued: list = []
    monkeypatch.setattr(
        "app.workers.tasks.poll_new_messages.apply_async",
        lambda args, countdown=0: enqueued.append(args),
    )

    tasks.enqueue_tracker_owner_polls.apply()

    assert enqueued == []


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
