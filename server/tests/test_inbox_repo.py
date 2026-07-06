from datetime import datetime, timezone
from sqlalchemy import select
from app.db.models import InboxThread, User
from app.inbox import inbox_repo


def _seed_user(db, uid="u1"):
    db.add(User(id=uid, email=f"{uid}@x.com", created_at=datetime.now(timezone.utc)))
    db.commit()


def _mk_user(db, uid="u1"):
    """Like _seed_user, but returns the created row (needed by tests that read
    user.id back, e.g. to seed threads/messages for a specific user)."""
    user = User(id=uid, email=f"{uid}@x.com", created_at=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    return user


def test_upsert_thread_and_message_creates_rows(db):
    _seed_user(db)
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT", subject="hi", bucket_id="default-important")
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="gT",
        gmail_message_id="gM", gmail_internal_date=2_000_000,
        gmail_history_id="42",
        to_addr="me@x.com", from_addr="alice@x.com", body_preview="hello",
    )
    db.commit()
    threads = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    assert len(threads) == 1
    assert threads[0].subject == "hi"
    assert threads[0].recent_message_id is not None


def test_upsert_message_updates_recent_message_pointer(db):
    _seed_user(db)
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT", subject="hi", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="gT", gmail_message_id="gM1",
        gmail_internal_date=1_000_000, gmail_history_id="10",
        to_addr=None, from_addr=None, body_preview="first",
    )
    inbox_repo.upsert_message(
        db, user_id="u1", gmail_thread_id="gT", gmail_message_id="gM2",
        gmail_internal_date=2_000_000, gmail_history_id="11",
        to_addr=None, from_addr=None, body_preview="second",
    )
    db.commit()
    [t] = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    recent = inbox_repo.get_message(db, user_id="u1", message_id=t.recent_message_id)
    assert recent.body_preview == "second"


def test_list_threads_orders_by_recent_message_internal_date_desc(db):
    _seed_user(db)
    for i, ts in enumerate([3_000_000, 1_000_000, 2_000_000]):
        inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id=f"gT{i}", subject=f"s{i}", bucket_id=None)
        inbox_repo.upsert_message(
            db, user_id="u1", gmail_thread_id=f"gT{i}", gmail_message_id=f"gM{i}",
            gmail_internal_date=ts, gmail_history_id=str(ts),
            to_addr=None, from_addr=None, body_preview=str(i),
        )
    db.commit()
    threads = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    assert [t.gmail_id for t in threads] == ["gT0", "gT2", "gT1"]


def test_upsert_thread_update_path_overwrites_subject_and_bucket(db):
    """Calling upsert_thread twice with the same (user_id, gmail_thread_id)
    must update the existing row, not create a duplicate."""
    _seed_user(db)
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT", subject="first", bucket_id=None)
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT", subject="second", bucket_id="default-important")
    db.commit()

    threads = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    assert len(threads) == 1
    assert threads[0].subject == "second"
    assert threads[0].bucket_id == "default-important"


def test_upsert_thread_update_with_none_preserves_existing_values(db):
    """Passing subject=None or bucket_id=None on the update path means 'leave it
    alone' — important so the worker can call upsert_thread with subject from a
    thread fetch that hasn't loaded headers yet."""
    _seed_user(db)
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT", subject="set", bucket_id="default-important")
    inbox_repo.upsert_thread(db, user_id="u1", gmail_thread_id="gT", subject=None, bucket_id=None)
    db.commit()

    threads = inbox_repo.list_threads(db, user_id="u1", limit=10, offset=0)
    assert threads[0].subject == "set"
    assert threads[0].bucket_id == "default-important"


def test_upsert_message_persists_body_labels_unread_and_activity(db):
    user = _mk_user(db)  # use the file's existing user-seeding helper/fixture
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id="gt1",
                             subject="s", bucket_id=None)
    msg = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt1", gmail_message_id="gm1",
        gmail_internal_date=111, gmail_history_id="7",
        to_addr="a@b.c", from_addr="x@y.z", body_preview="p",
        body_text="full body text", label_ids=["INBOX", "UNREAD"],
    )
    assert msg.body_text == "full body text"
    assert msg.labels == ["INBOX", "UNREAD"]
    assert msg.is_unread is True
    thread = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "gt1")).scalar_one()
    assert thread.last_activity_at == 111


def test_upsert_message_update_with_none_preserves_body_and_labels(db):
    """Passing body_text=None or label_ids=None on the update path means
    'leave it alone' — important so the worker can call upsert_message with
    partial updates that may not have the full body or label info yet."""
    user = _mk_user(db)
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id="gt", subject="s", bucket_id=None)

    # Initial upsert with body_text and labels
    msg = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt", gmail_message_id="gm",
        gmail_internal_date=100, gmail_history_id="10",
        to_addr="me@x.com", from_addr="alice@x.com", body_preview="preview",
        body_text="original body", label_ids=["INBOX", "UNREAD"],
    )
    assert msg.body_text == "original body"
    assert msg.labels == ["INBOX", "UNREAD"]
    assert msg.is_unread is True

    # Update with None values should preserve existing body_text and labels
    updated = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt", gmail_message_id="gm",
        gmail_internal_date=200, gmail_history_id="11",
        to_addr=None, from_addr=None, body_preview=None,
        body_text=None, label_ids=None,
    )
    assert updated.body_text == "original body"
    assert updated.labels == ["INBOX", "UNREAD"]
    assert updated.is_unread is True
    assert updated.gmail_internal_date == 200


def test_recompute_thread_pointers_skips_deleted_messages(db):
    user = _mk_user(db)
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id="gt2",
                             subject="s", bucket_id=None)
    old = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt2", gmail_message_id="gm-old",
        gmail_internal_date=100, gmail_history_id="1",
        to_addr=None, from_addr=None, body_preview=None)
    new = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt2", gmail_message_id="gm-new",
        gmail_internal_date=200, gmail_history_id="2",
        to_addr=None, from_addr=None, body_preview=None)
    thread = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "gt2")).scalar_one()
    assert thread.recent_message_id == new.id

    new.is_deleted = True
    inbox_repo.recompute_thread_pointers(db, thread=thread)
    assert thread.recent_message_id == old.id
    assert thread.last_activity_at == 100

    old.is_deleted = True
    inbox_repo.recompute_thread_pointers(db, thread=thread)
    assert thread.recent_message_id is None
    assert thread.last_activity_at is None


def test_list_threads_excludes_archived_by_default_and_sorts_by_activity(db):
    user = _mk_user(db)
    for gid, date in (("g-old", 100), ("g-new", 300), ("g-arch", 200)):
        inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id=gid,
                                 subject=gid, bucket_id=None)
        inbox_repo.upsert_message(
            db, user_id=user.id, gmail_thread_id=gid, gmail_message_id=f"m-{gid}",
            gmail_internal_date=date, gmail_history_id="1",
            to_addr=None, from_addr=None, body_preview=None)
    arch = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-arch")).scalar_one()
    arch.is_archived = True
    db.flush()  # session runs autoflush=False; make the mutation visible to list_threads' SELECT

    listed = inbox_repo.list_threads(db, user_id=user.id, limit=10, offset=0)
    assert [t.gmail_id for t in listed] == ["g-new", "g-old"]

    with_arch = inbox_repo.list_threads(db, user_id=user.id, limit=10, offset=0,
                                        include_archived=True)
    assert [t.gmail_id for t in with_arch] == ["g-new", "g-arch", "g-old"]
