from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import select, func
from app.config import get_settings
from app.db.models import Base, InboxMessage, InboxThread, User
from app.gmail.parser import ParsedMessage, ParsedThread
from app.inbox import inbox_repo
from app.task_engine import repo as task_repo
from app.workers import gmail_sync


@pytest.fixture(autouse=True)
def _stub_triage(monkeypatch):
    """Default stub for gmail_sync.triage (formerly gmail_sync.classify):
    no-fit bucket for every thread, zero tracker hits. Individual tests that
    need specific bucket/tracker outcomes override this via monkeypatch or
    `patch("app.workers.gmail_sync.triage", ...)` inside the test body."""
    monkeypatch.setattr(
        "app.workers.gmail_sync.triage",
        lambda threads, buckets, trackers, current, **kw: [(None, []) for _ in threads],
    )


def _parsed_thread(tid="g-t1"):
    """A minimal ParsedThread, for tests that call gmail_sync._triage_batch
    directly rather than through the full gmail-fetch + partial_sync_inbox
    path."""
    m = ParsedMessage(gmail_message_id=f"m_{tid}", gmail_thread_id=tid,
                      gmail_internal_date=1, gmail_history_id="1",
                      subject="s", from_addr="a@b", to_addr="me",
                      body_text="b", body_preview="b", label_ids=["INBOX"])
    return ParsedThread(gmail_thread_id=tid, subject="s", recent_internal_date=1, messages=[m])


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


def _fake_thread_payload(*, tid="gT1", mid="gM1", history_id="200", subject="hi", body="",
                          labels=("INBOX",)):
    # labels defaults to ("INBOX",) — realistic for a message reaching the
    # inbox at all; partial_sync_inbox now derives is_archived from this per
    # DEFECT 2a, so tests not specifically exercising archive-healing need a
    # believable label set to keep their upserted thread visible/non-archived.
    return {
        "id": tid, "messages": [{
            "id": mid, "threadId": tid, "internalDate": "1700000000000",
            "historyId": history_id, "labelIds": list(labels),
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


def test_fetch_history_records_paginates_across_pages(db):
    """Gmail pages users.history.list at ~100 records; a >100-record batch
    (bulk archive, stale cursor) spans multiple pages linked by
    nextPageToken. fetch_history_records must follow nextPageToken,
    accumulating records from every page, and return the FINAL page's
    historyId — not the first page's (which is stale once more pages
    remain)."""
    u = _seed_user(db)

    page1 = {
        "history": [{"id": "101", "messagesAdded": [{"message": {"id": "gM1", "threadId": "gT1"}}]}],
        "nextPageToken": "tok2",
        "historyId": "150",  # stale — must not be what's returned
    }
    page2 = {
        "history": [{"id": "201", "messagesAdded": [{"message": {"id": "gM2", "threadId": "gT2"}}]}],
        "historyId": "300",
    }

    def _fake_list(*, userId, startHistoryId, historyTypes, labelId, pageToken=None):
        assert userId == "me"
        assert startHistoryId == u.gmail_last_history_id
        assert historyTypes == ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]
        assert labelId == "INBOX"
        inner = MagicMock()
        if pageToken is None:
            inner.execute.return_value = page1
        else:
            assert pageToken == "tok2"
            inner.execute.return_value = page2
        return inner

    gmail = MagicMock()
    gmail.users().history().list.side_effect = _fake_list

    records, history_id = gmail_sync.fetch_history_records(
        gmail, start_history_id=u.gmail_last_history_id
    )

    assert records == page1["history"] + page2["history"]
    assert history_id == "300"


def test_fetch_history_records_raises_history_gone_when_page_cap_exceeded(db):
    """If nextPageToken keeps appearing past MAX_HISTORY_PAGES, treat the
    cursor as unrecoverable via pagination and raise HistoryGoneError — this
    reuses the existing 404 recovery path (caller falls back to
    full_sync_inbox, which reconciles correctly) instead of looping forever
    or silently dropping records beyond the cap.

    (poll_new_messages' behavior on HistoryGoneError — falling back to
    full_sync_inbox and publishing its result — is already covered by
    test_poll_new_messages_falls_back_to_full_sync_on_404 in test_tasks.py;
    this test only exercises fetch_history_records' own pagination cap.)"""
    u = _seed_user(db)
    call_count = 0

    def _fake_list(*, userId, startHistoryId, historyTypes, labelId, pageToken=None):
        nonlocal call_count
        call_count += 1
        inner = MagicMock()
        inner.execute.return_value = {
            "history": [{"id": str(call_count)}],
            "nextPageToken": f"tok{call_count}",  # never terminates
            "historyId": "999",
        }
        return inner

    gmail = MagicMock()
    gmail.users().history().list.side_effect = _fake_list

    with pytest.raises(gmail_sync.HistoryGoneError):
        gmail_sync.fetch_history_records(gmail, start_history_id=u.gmail_last_history_id)

    assert call_count == gmail_sync.MAX_HISTORY_PAGES


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


def test_full_sync_tolerates_per_thread_fetch_failure(db):
    """One stub's threads.get() raising (transient 500/network blip) must not
    500 the whole bootstrap sync, and — critically — must not cause the
    reconcile step to wrongly archive that stub's already-stored thread. A
    fetch failure means "we don't know its state this round," not "gmail no
    longer lists it"; the fix folds failed ids into the reconcile-archive
    exclusion set alongside listed_gmail_ids."""
    u = _seed_user(db, history_id=None)

    # g-flaky is already stored with activity (7000) inside the window this
    # listing will cover ([6000, 7000] once gT_ok's fixture is parsed) — if
    # its failed fetch were treated as "gmail dropped it" (i.e. excluded from
    # window_dates/listed_gmail_ids only), the reconcile-archive filter
    # (last_activity_at >= window_min AND gmail_id not in listed) would sweep
    # it up and wrongly archive a thread that's still very much in the inbox.
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="g-flaky", subject="flaky", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="g-flaky", gmail_message_id="m-flaky",
        gmail_internal_date=7000, gmail_history_id="700",
        to_addr=None, from_addr=None, body_preview="flaky",
    )
    db.commit()

    listing = {"threads": [{"id": "gT_ok"}, {"id": "g-flaky"}]}

    def _fake_threads_get(*, userId, id, format):
        if id == "g-flaky":
            inner = MagicMock()
            inner.execute.side_effect = RuntimeError("transient threads.get failure")
            return inner
        inner = MagicMock()
        inner.execute.return_value = {
            "id": "gT_ok",
            "messages": [{
                "id": "m_ok", "threadId": "gT_ok",
                "internalDate": "6000", "historyId": "999",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Subject", "value": "ok"}],
                    "body": {"data": ""},
                },
            }],
        }
        return inner

    gmail = MagicMock()
    gmail.users().threads().list().execute.return_value = listing
    gmail.users().threads().get.side_effect = _fake_threads_get

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        ids = gmail_sync.full_sync_inbox(db, user=u)  # must not raise

    ok_row = db.execute(select(InboxThread).where(
        InboxThread.user_id == "u1", InboxThread.gmail_id == "gT_ok")).scalar_one()
    assert ok_row.id in ids  # the other thread still ingests fine

    flaky_row = db.execute(select(InboxThread).where(
        InboxThread.user_id == "u1", InboxThread.gmail_id == "g-flaky")).scalar_one()
    assert flaky_row.is_archived is False, \
        "a fetch failure must not be treated as 'gmail no longer lists this thread'"
    assert flaky_row.id not in ids  # untouched: neither upserted nor archived


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


def test_partial_sync_archives_fetched_thread_without_inbox_label(db, user):
    """A messagesAdded record triggers a threads.get(format=full) fetch — the
    freshest possible label state available. If the fetched thread's messages
    carry no INBOX label, the thread must be marked is_archived=True right
    away instead of waiting for a labelsRemoved record (which may have been
    missed/dropped) or the next full sync."""
    records = [{"messagesAdded": [{"message": {"id": "gM1", "threadId": "gT1"}}]}]
    payload = _fake_thread_payload(tid="gT1", mid="gM1", history_id="200")
    payload["messages"][0]["labelIds"] = ["SENT"]  # no INBOX

    gmail = MagicMock()
    gmail.users().threads().get().execute.return_value = payload

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                      new_history_id="200")

    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "gT1")).scalar_one()
    assert t.is_archived is True


def test_partial_sync_heals_archived_thread_when_fetch_shows_inbox_label(db, user, seeded_thread):
    """A missed/dropped labelsAdded INBOX record can leave is_archived stale
    at True. When the same batch's messagesAdded path fetches the thread
    anyway (a fresh threads.get), the fetched label state is at-least-as-
    fresh as any label record processed earlier in the same batch (the flag
    loop runs before the fetch loop), so the derived write must win: healed
    back to is_archived=False even though no record explicitly said "INBOX
    added"."""
    seeded_thread.is_archived = True
    db.flush()  # tests must flush explicitly; production read helpers never do

    records = [{"messagesAdded": [{"message": {"id": "g-m3", "threadId": "g-t1"}}]}]
    payload = _fake_thread_payload(tid="g-t1", mid="g-m3", history_id="300")
    payload["messages"][0]["labelIds"] = ["INBOX"]

    gmail = MagicMock()
    gmail.users().threads().get().execute.return_value = payload

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                      new_history_id="300")

    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()
    assert t.is_archived is False


def test_partial_sync_heals_soft_deleted_message_when_refetched(db, user, seeded_thread):
    """A spurious/duplicated messagesDeleted record can soft-delete a message
    Gmail never actually removed. If a later messagesAdded record for the
    same thread triggers a fresh threads.get that still returns that
    message, a live Gmail fetch is definitional proof the message exists —
    is_deleted must be healed back to False rather than hiding the message
    forever."""
    m2 = db.execute(select(InboxMessage).where(
        InboxMessage.user_id == user.id, InboxMessage.gmail_id == "g-m2")).scalar_one()
    m2.is_deleted = True
    db.flush()  # tests must flush explicitly; production read helpers never do

    records = [{"messagesAdded": [{"message": {"id": "g-m2", "threadId": "g-t1"}}]}]

    def _msg(mid, date, history_id):
        return {
            "id": mid, "threadId": "g-t1", "internalDate": str(date),
            "historyId": history_id, "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "hi"}],
                "body": {"data": ""},
            },
        }

    payload = {"id": "g-t1", "messages": [_msg("g-m1", 100, "50"), _msg("g-m2", 200, "60")]}

    gmail = MagicMock()
    gmail.users().threads().get().execute.return_value = payload

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail):
        gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                      new_history_id="300")

    healed = db.execute(select(InboxMessage).where(
        InboxMessage.user_id == user.id, InboxMessage.gmail_id == "g-m2")).scalar_one()
    assert healed.is_deleted is False


# ---------------------------------------------------------------------------
# Task 5: _triage_batch (formerly _classify_batch) + dual-write task links
# ---------------------------------------------------------------------------


def test_triage_batch_zero_trackers_matches_old_classify_wiring(db):
    """Regression guarantee: with zero active trackers, _triage_batch's
    bucket-half wiring must be identical to the old _classify_batch — same
    buckets loaded, same postgres-read current bucket_id passed as the
    stability hint, same passthrough of whatever triage() returns. Stubs
    classify.triage (patched as gmail_sync.triage) to prove the wiring itself
    rather than depending on live prompt/model behavior."""
    u = _seed_user(db)
    inbox_repo.upsert_thread(db, user_id=u.id, gmail_thread_id="g-t1", subject="hi",
                             bucket_id="old-bucket")
    db.commit()

    captured = {}

    def _fake_triage(threads, buckets, trackers, current, **kw):
        captured["trackers"] = trackers
        captured["current"] = current
        return [("new-bucket", [])]

    with patch("app.workers.gmail_sync.triage", side_effect=_fake_triage):
        out = gmail_sync._triage_batch(db, user_id=u.id, parsed_list=[_parsed_thread("g-t1")])

    assert out == [("new-bucket", [])]
    assert captured["trackers"] == []              # zero active trackers for this user
    assert captured["current"] == ["old-bucket"]    # stability hint read from postgres


def test_partial_sync_writes_link_above_threshold_and_skips_below(db, user, seeded_thread):
    """One active tracker scored above TASK_LINK_CONFIDENCE gets a link row
    with its confidence; another scored below the threshold gets none."""
    task_hi = task_repo.create_task(db, user_id=user.id, name="Hi", goal="", criteria="",
                                    state_schema=None)
    task_lo = task_repo.create_task(db, user_id=user.id, name="Lo", goal="", criteria="",
                                    state_schema=None)
    db.commit()
    threshold = get_settings().task_link_confidence

    records = [{"messagesAdded": [{"message": {"id": "g-m3", "threadId": "g-t1"}}]}]
    payload = _fake_thread_payload(tid="g-t1", mid="g-m3", history_id="300")
    gmail = MagicMock()
    gmail.users().threads().get().execute.return_value = payload

    def _fake_triage(threads, buckets, trackers, current, **kw):
        return [(None, [(task_hi.id, 90), (task_lo.id, threshold - 1)])]

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail), \
         patch("app.workers.gmail_sync.triage", side_effect=_fake_triage):
        gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                      new_history_id="300")

    thread = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()

    hi_link = task_repo.get_link(db, task_id=task_hi.id, thread_id=thread.id)
    assert hi_link is not None
    assert hi_link.origin == "llm"
    assert hi_link.state == "attached"
    assert hi_link.confidence == 90

    lo_link = task_repo.get_link(db, task_id=task_lo.id, thread_id=thread.id)
    assert lo_link is None, "below-threshold task hit must not create a link row"


def test_partial_sync_does_not_overwrite_existing_user_origin_link(db, user, seeded_thread):
    """A user's explicit detach (origin='user') must survive a later sync
    that triages the same thread as a high-confidence match for the same
    task — upsert_link's sticky rule protects it."""
    task = task_repo.create_task(db, user_id=user.id, name="T", goal="", criteria="",
                                 state_schema=None)
    db.commit()

    task_repo.upsert_link(db, task_id=task.id, thread_id=seeded_thread.id, user_id=user.id,
                          origin="user", state="detached")
    db.commit()

    records = [{"messagesAdded": [{"message": {"id": "g-m3", "threadId": "g-t1"}}]}]
    payload = _fake_thread_payload(tid="g-t1", mid="g-m3", history_id="300")
    gmail = MagicMock()
    gmail.users().threads().get().execute.return_value = payload

    def _fake_triage(threads, buckets, trackers, current, **kw):
        return [(None, [(task.id, 95)])]

    with patch("app.workers.gmail_sync.get_gmail_client", return_value=gmail), \
         patch("app.workers.gmail_sync.triage", side_effect=_fake_triage):
        gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                      new_history_id="300")

    link = task_repo.get_link(db, task_id=task.id, thread_id=seeded_thread.id)
    assert link.origin == "user"
    assert link.state == "detached", \
        "sticky rule: an origin='user' detached link must not be re-attached by an llm upsert"
