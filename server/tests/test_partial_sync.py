from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import select, func
from app.db.models import Base, InboxMessage, InboxThread, User
from app.inbox import inbox_repo
from app.workers import gmail_sync


@pytest.fixture(autouse=True)
def _stub_classify(monkeypatch):
    monkeypatch.setattr("app.workers.gmail_sync.classify",
                        lambda threads, buckets, current: [None] * len(threads))


def _seed_user(db, *, history_id="100"):
    Base.metadata.create_all(db.get_bind())
    u = User(
        id="u1", email="a@b.com", created_at=datetime.now(timezone.utc),
        gmail_last_history_id=history_id,
    )
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def user(db):
    return _seed_user(db)


@pytest.fixture
def seeded_thread(db, user):
    """Thread g-t1 with two messages: g-m1 (date 100), g-m2 (date 200)."""
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id="g-t1", subject="hi", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="g-t1", gmail_message_id="g-m1",
        gmail_internal_date=100, gmail_history_id="50",
        to_addr=None, from_addr=None, body_preview="m1",
    )
    inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="g-t1", gmail_message_id="g-m2",
        gmail_internal_date=200, gmail_history_id="60",
        to_addr=None, from_addr=None, body_preview="m2",
    )
    db.commit()
    return db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()


def _fake_thread_payload(*, tid="gT1", mid="gM1", history_id="200", subject="hi", body=""):
    return {
        "id": tid, "messages": [{
            "id": mid, "threadId": tid, "internalDate": "1700000000000",
            "historyId": history_id,
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": subject}],
                "body": {"data": body},
            },
        }],
    }


def test_partial_sync_uses_passed_records_without_calling_history(db):
    """When history_records is given, partial_sync must NOT call users.history.list."""
    u = _seed_user(db)
    gmail = MagicMock()
    gmail.users().threads().get().execute.return_value = _fake_thread_payload(tid="gT1", history_id="200")

    records = [{"id": "200", "messagesAdded": [{"message": {"id": "gM1", "threadId": "gT1"}}]}]

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.partial_sync_inbox(
            db, user=u, history_records=records, new_history_id="200",
        )

    [thread] = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    assert thread.gmail_id == "gT1"
    # The worker returns internal InboxThread.id (UUID hex), not gmail_thread_id.
    # /api/threads/batch filters by InboxThread.id, so SSE-published ids must
    # match that column.
    assert ids == [thread.id]
    assert db.get(User, "u1").gmail_last_history_id == "200"
    # Critical: history.list MUST NOT have been called when records were passed.
    gmail.users().history().list.assert_not_called()


def test_partial_sync_calls_history_when_records_none(db):
    """When history_records is None, partial_sync must fetch via history.list."""
    u = _seed_user(db)
    gmail = MagicMock()
    gmail.users().history().list.return_value.execute.return_value = {
        "history": [{"id": "200", "messagesAdded": [{"message": {"id": "gM1", "threadId": "gT1"}}]}],
        "historyId": "200",
    }
    gmail.users().threads().get().execute.return_value = _fake_thread_payload()

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.partial_sync_inbox(db, user=u)

    [thread] = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    assert ids == [thread.id]  # internal InboxThread.id, not gmail_thread_id
    gmail.users().history().list.assert_called()


def test_partial_sync_returns_empty_when_records_empty(db):
    """Empty records list short-circuits before any thread fetch."""
    u = _seed_user(db)
    gmail = MagicMock()

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.partial_sync_inbox(
            db, user=u, history_records=[], new_history_id="100",
        )

    assert ids == []
    gmail.users().threads().get.assert_not_called()


def test_fetch_history_records_translates_404_to_history_gone_error(db):
    """fetch_history_records must raise HistoryGoneError on a 404 from gmail."""
    u = _seed_user(db)

    class _FakeResp:
        status = 404
        reason = "Not Found"

    gmail = MagicMock()
    gmail.users().history().list.return_value.execute.side_effect = HttpError(
        resp=_FakeResp(), content=b'{"error": "not found"}'
    )

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        with pytest.raises(gmail_sync.HistoryGoneError):
            gmail_sync.fetch_history_records(gmail, start_history_id=u.gmail_last_history_id)


def test_full_sync_inbox_pulls_latest_200_threads_and_writes(db):
    u = _seed_user(db, history_id=None)

    listing = {"threads": [{"id": f"gT{i}"} for i in range(3)]}
    def _thread_fixture(thread_id: str) -> dict:
        return {
            "id": thread_id,
            "messages": [{
                "id": f"m_{thread_id}", "threadId": thread_id,
                "internalDate": "1700000000000",
                "historyId": "999",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": thread_id}],
                    "body": {"data": ""},
                },
            }],
        }

    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = listing

    # Capture id from the .get() call args rather than .execute() kwargs —
    # the real Gmail API does not accept id in execute(), only in get().
    def _fake_threads_get(*, userId, id, format):
        inner = MagicMock()
        inner.execute.return_value = _thread_fixture(id)
        return inner
    gmail.users().threads().get.side_effect = _fake_threads_get

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.full_sync_inbox(db, user=u)

    threads = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    assert {t.gmail_id for t in threads} == {"gT0", "gT1", "gT2"}
    # full sync returns internal InboxThread.id, not gmail_thread_id (the SSE
    # publish path forwards these to /api/threads/batch which filters by .id).
    assert set(ids) == {t.id for t in threads}
    # full sync must populate last_history_id from the messages it ingested
    assert db.get(User, "u1").gmail_last_history_id == "999"


def test_full_sync_never_deletes_stale_threads(db):
    """Full sync is a reconciling upsert, not a wipe: pre-existing rows whose
    activity is outside (or incomparable to) the newly listed window must
    survive untouched — never deleted — even though gmail's new listing
    doesn't mention them. (The "vanished from inside the window → archived"
    case is covered by test_full_sync_reconciles_instead_of_wiping.)"""
    u = _seed_user(db, history_id=None)

    # Seed two stale threads that gmail's listing won't include. STALE_A's
    # activity (1) predates the listed window (~1.7e12); STALE_B has no
    # messages at all (last_activity_at is NULL) — neither is comparable to
    # the window, so reconciliation must leave both alone.
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="STALE_A", subject="old", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="STALE_A", gmail_message_id="m_stale_a",
        gmail_internal_date=1, gmail_history_id="1",
        to_addr=None, from_addr=None, body_preview="old",
    )
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="STALE_B", subject="old", bucket_id=None)
    db.commit()
    assert {t.gmail_id for t in inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)} \
        == {"STALE_A", "STALE_B"}

    listing = {"threads": [{"id": "gT_NEW"}]}
    new_thread = {
        "id": "gT_NEW",
        "messages": [{
            "id": "m_new", "threadId": "gT_NEW",
            "internalDate": "1700000000000", "historyId": "500",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "fresh"}],
                "body": {"data": ""},
            },
        }],
    }
    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = listing
    gmail.users().threads().get().execute.return_value = new_thread

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.full_sync_inbox(db, user=u)

    new_row = db.execute(select(InboxThread).where(
        InboxThread.user_id == "u1", InboxThread.gmail_id == "gT_NEW")).scalar_one()
    assert ids == [new_row.id]  # internal InboxThread.id, not gmail_thread_id

    surviving = {t.gmail_id for t in inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)}
    assert surviving == {"STALE_A", "STALE_B", "gT_NEW"}, \
        f"reconciling upsert must never delete rows: {surviving}"

    # No rows deleted at the storage level either (not just hidden by the
    # is_archived filter in list_threads).
    assert db.execute(select(func.count(InboxThread.id)).where(
        InboxThread.user_id == "u1")).scalar_one() == 3


def test_full_sync_reconciles_instead_of_wiping(db):
    """Pre-existing thread NOT in the new listing but older than the listed
    window must survive untouched; one inside the window but absent from the
    listing must be marked archived (it left the inbox while the cursor was
    dead); listed threads upsert idempotently (no duplicate rows)."""
    u = _seed_user(db, history_id=None)

    # g-ancient: activity (100) predates the window the new listing covers
    # (4000..6000 below). Outside the window is out of scope — must survive
    # completely untouched.
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="g-ancient", subject="ancient", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="g-ancient", gmail_message_id="m-ancient",
        gmail_internal_date=100, gmail_history_id="100",
        to_addr=None, from_addr=None, body_preview="ancient",
    )

    # g-gone: activity (5000) falls inside the window [4000, 6000] the new
    # listing covers, but the new listing does not include it — it left the
    # inbox (archived/deleted remotely) while our cursor was dead. Must be
    # marked archived and reported back so SSE consumers evict it.
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="g-gone", subject="gone", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="g-gone", gmail_message_id="m-gone",
        gmail_internal_date=5000, gmail_history_id="200",
        to_addr=None, from_addr=None, body_preview="gone",
    )

    # gT1: already exists (stale subject) AND is present in the new listing —
    # proves the upsert is idempotent (updates in place, no duplicate row).
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT1", subject="stale subject", bucket_id=None)
    db.commit()

    def _fake_thread(tid: str, internal_date: int, subject: str) -> dict:
        return {
            "id": tid,
            "messages": [{
                "id": f"m_{tid}", "threadId": tid,
                "internalDate": str(internal_date), "historyId": "999",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": subject}],
                    "body": {"data": ""},
                },
            }],
        }

    listing = {"threads": [{"id": "gT1"}, {"id": "gT2"}]}
    fixtures = {
        "gT1": _fake_thread("gT1", 4000, "fresh gT1"),
        "gT2": _fake_thread("gT2", 6000, "fresh gT2"),
    }

    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = listing

    # Capture id from the .get() call args (matches the fixture pattern used
    # by test_full_sync_inbox_pulls_latest_200_threads_and_writes above).
    def _fake_threads_get(*, userId, id, format):
        inner = MagicMock()
        inner.execute.return_value = fixtures[id]
        return inner
    gmail.users().threads().get.side_effect = _fake_threads_get

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.full_sync_inbox(db, user=u)

    old = db.execute(select(InboxThread).where(
        InboxThread.user_id == u.id, InboxThread.gmail_id == "g-ancient")).scalar_one()
    assert old.is_archived is False  # outside window: untouched

    gone = db.execute(select(InboxThread).where(
        InboxThread.user_id == u.id, InboxThread.gmail_id == "g-gone")).scalar_one()
    assert gone.is_archived is True  # inside window, absent from listing
    assert gone.id in ids            # published so clients evict it

    listed = db.execute(select(InboxThread).where(
        InboxThread.user_id == u.id, InboxThread.gmail_id == "gT1")).scalar_one()
    assert listed.subject == "fresh gT1"  # idempotent upsert updates in place

    # no rows were deleted
    assert db.execute(select(func.count(InboxThread.id)).where(
        InboxThread.user_id == u.id)).scalar_one() >= 4


def test_full_sync_window_ignores_messageless_threads(db):
    """A malformed listed thread with zero messages must not collapse the
    reconcile window to 0. parser.assemble_thread returns recent_internal_date=0
    for a thread whose raw_messages is empty; if window_min trusted that value,
    the reconcile filter `last_activity_at >= 0` would match almost every
    stored thread, causing mass false-archival in a single sync."""
    u = _seed_user(db, history_id=None)

    # Stored thread with real (if old) activity. Its last_activity_at (100) is
    # still >= 0, so under the bug (window_min collapsed to 0 by the
    # messageless thread below) it would be incorrectly swept into the
    # archive filter even though it's nowhere near the listed window (5000).
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="g-old", subject="old", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="g-old", gmail_message_id="m-old",
        gmail_internal_date=100, gmail_history_id="50",
        to_addr=None, from_addr=None, body_preview="old",
    )
    db.commit()

    listing = {"threads": [{"id": "gT_NORMAL"}, {"id": "gT_EMPTY"}]}

    def _fake_threads_get(*, userId, id, format):
        inner = MagicMock()
        if id == "gT_NORMAL":
            inner.execute.return_value = {
                "id": "gT_NORMAL",
                "messages": [{
                    "id": "m_normal", "threadId": "gT_NORMAL",
                    "internalDate": "5000", "historyId": "999",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [{"name": "Subject", "value": "normal"}],
                        "body": {"data": ""},
                    },
                }],
            }
        else:
            # Malformed listing entry: gmail returned zero messages for this
            # thread id, so assemble_thread yields recent_internal_date=0.
            inner.execute.return_value = {"id": "gT_EMPTY", "messages": []}
        return inner

    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = listing
    gmail.users().threads().get.side_effect = _fake_threads_get

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        gmail_sync.full_sync_inbox(db, user=u)

    old = db.execute(select(InboxThread).where(
        InboxThread.user_id == "u1", InboxThread.gmail_id == "g-old")).scalar_one()
    assert old.is_archived is False, \
        "messageless listed thread must not collapse window_min to 0 and mass-archive"


def test_full_sync_unarchives_reappearing_thread(db):
    """Full sync's labelIds=["INBOX"] listing is authoritative in both
    directions: any thread it lists IS in the inbox right now, even if a
    stale is_archived=True flag says otherwise (e.g. the thread was archived,
    then un-archived in gmail before our cursor caught up). Nothing else in
    the sync path ever clears is_archived, so full sync must."""
    u = _seed_user(db, history_id=None)

    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="g-reappear", subject="back", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="g-reappear", gmail_message_id="m-reappear",
        gmail_internal_date=1000, gmail_history_id="10",
        to_addr=None, from_addr=None, body_preview="back",
    )
    thread = db.execute(select(InboxThread).where(
        InboxThread.user_id == "u1", InboxThread.gmail_id == "g-reappear")).scalar_one()
    thread.is_archived = True
    db.flush()  # tests must flush explicitly; production read helpers never do
    db.commit()

    listing = {"threads": [{"id": "g-reappear"}]}
    fixture = {
        "id": "g-reappear",
        "messages": [{
            "id": "m-reappear2", "threadId": "g-reappear",
            "internalDate": "6000", "historyId": "999",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "back again"}],
                "body": {"data": ""},
            },
        }],
    }

    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = listing
    gmail.users().threads().get().execute.return_value = fixture

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.full_sync_inbox(db, user=u)

    row = db.execute(select(InboxThread).where(
        InboxThread.user_id == "u1", InboxThread.gmail_id == "g-reappear")).scalar_one()
    assert row.is_archived is False
    assert row.id in ids


def test_partial_sync_soft_deletes_message_and_recomputes(db, user, seeded_thread):
    records = [{"messagesDeleted": [{"message": {"id": "g-m2", "threadId": "g-t1"}}]}]
    with patch("app.workers.gmail_sync.get_gmail_client", return_value=MagicMock()):
        ids = gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                            new_history_id="99")
    m2 = db.execute(select(InboxMessage).where(
        InboxMessage.user_id == user.id, InboxMessage.gmail_id == "g-m2")).scalar_one()
    assert m2.is_deleted is True            # soft, row survives
    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()
    assert t.last_activity_at == 100        # pointer recomputed past the deletion
    assert t.id in ids


def test_partial_sync_mirrors_archive_and_unread(db, user, seeded_thread):
    records = [
        {"labelsRemoved": [{"message": {"id": "g-m1", "threadId": "g-t1"},
                            "labelIds": ["INBOX"]}]},
        {"labelsAdded":   [{"message": {"id": "g-m1", "threadId": "g-t1"},
                            "labelIds": ["UNREAD"]}]},
    ]
    with patch("app.workers.gmail_sync.get_gmail_client", return_value=MagicMock()):
        ids = gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                            new_history_id="100")
    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()
    assert t.is_archived is True            # INBOX label removed → archived
    m1 = db.execute(select(InboxMessage).where(
        InboxMessage.user_id == user.id, InboxMessage.gmail_id == "g-m1")).scalar_one()
    assert m1.is_unread is True
    assert t.id in ids


def test_partial_sync_unarchives_on_inbox_label_added(db, user, seeded_thread):
    # pre-archive the thread, then deliver labelsAdded INBOX
    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()
    t.is_archived = True
    records = [{"labelsAdded": [{"message": {"id": "g-m1", "threadId": "g-t1"},
                                 "labelIds": ["INBOX"]}]}]
    with patch("app.workers.gmail_sync.get_gmail_client", return_value=MagicMock()):
        gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                      new_history_id="101")
    assert t.is_archived is False
