"""Gmail sync logic, called by celery tasks.

Three entry points:
 - fetch_history_records: thin wrapper around users.history.list. Translates a
   404 (cursor older than gmail's ~30-day window) into HistoryGoneError so the
   caller can fall back to a full sync.
 - partial_sync_inbox: incremental writer. Optionally takes pre-fetched
   history_records so poll_new_messages can call history.list once and pass
   the result through (per spec: "called by poll_new_messages. also reusable
   for full sync. if history_records is null, fetch via users.history.list").
 - full_sync_inbox: bootstrap / 404-recovery. Reconciling upsert against the
   latest 200 threads — never deletes rows; stored non-archived threads that
   vanished from the listed window are marked is_archived=True instead.

All three commit internally and return the list of gmail_thread_ids that were
touched. Returning ids only (not full thread payloads) keeps callers — the SSE
publish path in tasks.py — small: the data is in postgres for the api to read.
"""

import logging

from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.db.models import Bucket, InboxMessage, InboxThread, User
from app.inbox import inbox_repo, bucket_repo
from app.llm.classify import classify
from app.gmail.client import get_gmail_client
from app.gmail.parser import assemble_thread, ParsedThread


log = logging.getLogger(__name__)

# Gmail pages users.history.list at ~100 records per response. With the
# widened historyTypes (messageAdded/messageDeleted/labelAdded/labelRemoved),
# a single poll interval can realistically produce >100 records (a bulk
# archive, or a stale cursor after downtime), so fetch_history_records must
# paginate. This bounds that pagination: past MAX_HISTORY_PAGES (~2000
# records) we stop trusting the cursor to catch up via pagination and fall
# back to the existing HistoryGoneError → full_sync_inbox recovery path
# instead of looping indefinitely.
MAX_HISTORY_PAGES = 20


class HistoryGoneError(Exception):
    """users.history.list returned 404. The startHistoryId is older than the
    ~30-day window Gmail keeps. Caller should fall back to a full sync."""


def _upsert_thread_with_messages(
    db: Session, *, user_id: str, parsed: ParsedThread, bucket_id: str | None,
) -> str:
    """Write a thread and all its messages to postgres with a precomputed bucket_id.

    Classification is NOT done here — caller must call _classify_batch first and
    pass the resolved bucket_id. Caller is responsible for the surrounding
    transaction (db.commit happens in partial_sync_inbox / full_sync_inbox).

    Returns the internal InboxThread.id (UUID hex). This is the id the api +
    client use everywhere to identify a thread; the worker returns it so the
    SSE publish path can carry the same identifier — sending gmail_thread_id
    instead would make /api/threads/batch (which filters by InboxThread.id)
    return zero rows.
    """
    thread = inbox_repo.upsert_thread(
        db, user_id=user_id, gmail_thread_id=parsed.gmail_thread_id,
        subject=parsed.subject, bucket_id=bucket_id,
    )
    for m in parsed.messages:
        inbox_repo.upsert_message(
            db, user_id=user_id, gmail_thread_id=parsed.gmail_thread_id,
            gmail_message_id=m.gmail_message_id,
            gmail_internal_date=m.gmail_internal_date,
            gmail_history_id=m.gmail_history_id,
            to_addr=m.to_addr, from_addr=m.from_addr, body_preview=m.body_preview,
            body_text=m.body_text, label_ids=m.label_ids,
        )
    return thread.id


def _classify_batch(db: Session, *, user_id: str, parsed_list: list[ParsedThread]) -> list[str | None]:
    """Classify a batch of parsed threads in one parallel LLM call.

    Loads active buckets for the user once, then fetches each thread's existing
    bucket_id from postgres as a stability hint (prevents needless re-routing
    of already-classified threads). Delegates to classify() which runs all
    _classify_one coroutines concurrently under the shared semaphore.
    Returns a list of bucket_ids (or None) in the same order as parsed_list.
    """
    if not parsed_list:
        return []
    buckets = bucket_repo.list_active(db, user_id=user_id)
    current = []
    for parsed in parsed_list:
        existing = db.execute(
            select(InboxThread.bucket_id).where(
                InboxThread.user_id == user_id,
                InboxThread.gmail_id == parsed.gmail_thread_id,
            )
        ).scalar_one_or_none()
        current.append(existing)
    return classify(parsed_list, buckets, current, user_id=user_id)


def fetch_history_records(
    gmail_client, *, start_history_id: str
) -> tuple[list[dict], str | None]:
    """Call users.history.list, following nextPageToken until Gmail reports no
    more pages, and return (all records across every page, the final page's
    historyId).

    Gmail pages history.list responses at ~100 records; returning only the
    first page's records/historyId (as this function used to) would silently
    and permanently lose any records on unfetched later pages — the cursor
    would advance past history the caller never saw. So this loops on
    nextPageToken, accumulating records from every page, up to
    MAX_HISTORY_PAGES. If a nextPageToken still remains once the cap is hit,
    the cursor is treated as unrecoverable via pagination and HistoryGoneError
    is raised — reusing the existing 404 recovery path (caller falls back to
    full_sync_inbox, which reconciles correctly) instead of looping forever or
    dropping the remainder.

    Raises HistoryGoneError when gmail returns 404 on any page fetch (the
    cursor is past the retention window). All other HttpErrors propagate.
    """
    log.info("fetch_history_records: start_history_id=%s", start_history_id)
    all_records: list[dict] = []
    new_history_id: str | None = None
    page_token: str | None = None

    for page_num in range(1, MAX_HISTORY_PAGES + 1):
        kwargs = dict(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            # Without this, sending a message fires a messageAdded event for the SENT
            # label and gets ingested as if it were inbox mail, which then surfaces in
            # the UI as a thread "from a different address". Singular labelId per the
            # users.history.list API contract (vs labelIds plural on threads.list).
            labelId="INBOX",
        )
        if page_token is not None:
            kwargs["pageToken"] = page_token
        try:
            resp = gmail_client.users().history().list(**kwargs).execute()
        except HttpError as e:
            if getattr(e.resp, "status", None) == 404:
                raise HistoryGoneError() from e
            raise
        page_records = resp.get("history", []) or []
        all_records.extend(page_records)
        new_history_id = resp.get("historyId")
        page_token = resp.get("nextPageToken")
        log.info(
            "fetch_history_records: page %d got %d records, nextPageToken=%s",
            page_num, len(page_records), bool(page_token),
        )
        if not page_token:
            log.info(
                "fetch_history_records: got %d records across %d page(s), new historyId=%s",
                len(all_records), page_num, new_history_id,
            )
            return all_records, new_history_id

    # Exhausted MAX_HISTORY_PAGES pages and a nextPageToken still remains —
    # more history than we're willing to paginate through in one call. Falls
    # back to full_sync_inbox via the same recovery path as a 404.
    log.warning(
        "fetch_history_records: exceeded MAX_HISTORY_PAGES=%d with nextPageToken still "
        "present; treating cursor as gone", MAX_HISTORY_PAGES,
    )
    raise HistoryGoneError()


def partial_sync_inbox(
    db: Session, *,
    user: User,
    history_records: list[dict] | None = None,
    new_history_id: str | None = None,
) -> list[str]:
    """Incremental sync.

    If history_records is None, fetches them via fetch_history_records (which
    may raise HistoryGoneError; caller decides what to do with that). When the
    caller already has the records — e.g. poll_new_messages just called
    history.list to decide whether to publish — they pass them through to
    avoid a redundant API call.

    Writes touched threads + their messages to postgres in one transaction.
    Handles four history record shapes: messagesAdded (fetch + classify +
    upsert), messagesDeleted (soft-delete + recompute pointers + archive-when-
    empty), labelsAdded/labelsRemoved for INBOX (un-archive/archive, or ingest
    a previously-unseen thread when INBOX is added), and labelsAdded/Removed
    for UNREAD (flip InboxMessage.is_unread). Returns the list of internal
    InboxThread.id values (UUID hex) touched by ANY of the above — NOT
    gmail_thread_ids. The SSE publish path forwards these to
    /api/threads/batch, which filters by InboxThread.id.

    Self-healing, not just full-sync-dependent: every messagesAdded fetch
    (threads.get format="full") re-derives is_archived from the fetched
    labels and writes it, so a missed/dropped labelsAdded/Removed INBOX
    record heals immediately instead of waiting for the next full sync.
    Likewise, upsert_message's update path (inbox_repo.upsert_message) clears
    is_deleted whenever a message is re-seen via a live fetch, healing a
    spurious/duplicated messagesDeleted record.
    """
    records_provided = history_records is not None
    log.info(
        "partial_sync_inbox: user=%s records_provided=%s",
        user.id, records_provided,
    )
    gmail = get_gmail_client(db, user)

    if history_records is None:
        history_records, new_history_id = fetch_history_records(
            gmail, start_history_id=user.gmail_last_history_id or "0",
        )

    if not history_records:
        log.info("partial_sync_inbox: user=%s no history records → returning empty", user.id)
        return []

    touched_gmail_ids: set[str] = set()      # need a threads.get + full upsert
    flag_touched_internal_ids: set[str] = set()  # in-place flag updates only

    def _local_thread(gmail_thread_id: str) -> InboxThread | None:
        return db.execute(select(InboxThread).where(
            InboxThread.user_id == user.id,
            InboxThread.gmail_id == gmail_thread_id)).scalar_one_or_none()

    def _local_message(gmail_message_id: str) -> InboxMessage | None:
        return db.execute(select(InboxMessage).where(
            InboxMessage.user_id == user.id,
            InboxMessage.gmail_id == gmail_message_id)).scalar_one_or_none()

    for record in history_records:
        for added in record.get("messagesAdded", []) or []:
            tid = (added.get("message") or {}).get("threadId")
            if tid:
                touched_gmail_ids.add(tid)

        for deleted in record.get("messagesDeleted", []) or []:
            gm_id = (deleted.get("message") or {}).get("id")
            row = _local_message(gm_id) if gm_id else None
            if row is None:
                continue
            # Soft delete: task evidence (Phase 2) must survive Gmail deletions.
            row.is_deleted = True
            thread = db.get(InboxThread, row.thread_id)
            if thread is not None:
                inbox_repo.recompute_thread_pointers(db, thread=thread)
                if thread.recent_message_id is None:
                    thread.is_archived = True  # every message gone → leave the inbox view
                flag_touched_internal_ids.add(thread.id)

        for key, label_present in (("labelsAdded", True), ("labelsRemoved", False)):
            for change in record.get(key, []) or []:
                labels = set(change.get("labelIds", []) or [])
                msg = change.get("message") or {}
                if "INBOX" in labels and msg.get("threadId"):
                    thread = _local_thread(msg["threadId"])
                    if thread is not None:
                        thread.is_archived = not label_present
                        flag_touched_internal_ids.add(thread.id)
                    elif label_present:
                        # INBOX added to a thread we don't hold → ingest it.
                        touched_gmail_ids.add(msg["threadId"])
                if "UNREAD" in labels and msg.get("id"):
                    row = _local_message(msg["id"])
                    if row is not None:
                        row.is_unread = label_present
                        flag_touched_internal_ids.add(row.thread_id)

    log.info(
        "partial_sync_inbox: user=%s touched %d thread ids, fetching each",
        user.id, len(touched_gmail_ids),
    )

    # Parse all touched threads first, then classify in one batch call, then upsert.
    # This lets classify() parallelize LLM calls across all threads in a single gather().
    # Per-thread try/except tolerates 404s on threads that were deleted between when
    # Gmail emitted the history record and when we fetch them — without it the whole
    # task crashes and the history cursor never advances past the deleted thread,
    # causing an infinite 30s retry loop on the next beat. Matches the pattern in
    # extend_inbox_history below.
    parsed_list: list[ParsedThread] = []
    for tid in touched_gmail_ids:
        log.info("partial_sync_inbox: fetching thread %s for user=%s", tid, user.id)
        try:
            thread_resp = gmail.users().threads().get(userId="me", id=tid, format="full").execute()
        except Exception:
            log.exception("partial_sync_inbox: threads.get failed for %s; skipping", tid)
            continue
        parsed_list.append(assemble_thread(thread_id=tid, raw_messages=thread_resp.get("messages", []) or []))

    bucket_ids = _classify_batch(db, user_id=user.id, parsed_list=parsed_list)
    internal_ids = []
    for p, b in zip(parsed_list, bucket_ids):
        internal_id = _upsert_thread_with_messages(db, user_id=user.id, parsed=p, bucket_id=b)
        internal_ids.append(internal_id)

        # Heal is_archived from the fetched thread's own label state. The
        # flag-record loop above (labelsAdded/labelsRemoved INBOX) runs BEFORE
        # this fetch loop, so a threads.get(format="full") result here is
        # at-least-as-fresh as any label record earlier in the same batch —
        # this derived write correctly wins over (and heals) a missed/dropped
        # label record instead of waiting for the next full sync.
        inbox_present = any("INBOX" in m.label_ids for m in p.messages)
        thread_row = db.get(InboxThread, internal_id)
        if thread_row is not None:
            thread_row.is_archived = not inbox_present

    # Merge in threads touched only by an in-place flag flip (soft-delete,
    # archive/unarchive, unread) — they never went through the upsert loop
    # above but still need to be reported so SSE consumers see the change.
    internal_ids = list({*internal_ids, *flag_touched_internal_ids})

    if new_history_id:
        inbox_repo.update_user_history_id(db, user_id=user.id, history_id=str(new_history_id))
    db.commit()
    log.info(
        "partial_sync_inbox: user=%s done, %d threads upserted",
        user.id, len(internal_ids),
    )
    return internal_ids


def full_sync_inbox(db: Session, *, user: User) -> list[str]:
    """Bootstrap / 404-recovery sync.

    Reconciling upsert, NOT a wipe: repopulates from the 200 most-recently-
    active gmail threads, upserting each (never deleting). The listing
    (labelIds=["INBOX"]) is authoritative in both directions within the
    window it covers: listed threads that were previously is_archived=True
    are cleared back to False (they reappeared in the inbox), and stored,
    non-archived threads whose last activity falls inside the window we just
    listed but which gmail no longer returns are marked is_archived=True —
    they left the inbox while our cursor was dead. Threads outside that
    window are untouched; they're simply out of scope for this listing.

    The wipe-then-repopulate approach this replaced was removed because
    Phase 2 task tables FK onto inbox_threads.id — deleting rows here would
    orphan task evidence, and HistoryGoneError recovery (which calls this
    function) must not be destructive.

    Returns the list of internal InboxThread.id values (UUID hex) that were
    upserted or archived — NOT gmail_thread_ids. The SSE publish path forwards
    these to /api/threads/batch, which filters by InboxThread.id. Commits
    internally.
    """
    log.info("full_sync_inbox: start user=%s", user.id)
    gmail = get_gmail_client(db, user)

    # labelIds=["INBOX"] scopes to threads with at least one inbox-labeled message,
    # matching Gmail's own "Inbox" view. Without it threads.list returns the All Mail
    # universe (SENT, DRAFTS, etc.), so anything you sent surfaces in the inbox table.
    listing = gmail.users().threads().list(
        userId="me", maxResults=200, labelIds=["INBOX"],
    ).execute()
    thread_stubs = listing.get("threads", []) or []
    log.info("full_sync_inbox: user=%s listing returned %d thread stubs", user.id, len(thread_stubs))

    # Parse all threads first, then classify in one batch call, then upsert.
    # This lets classify() parallelize LLM calls across all threads in a single gather().
    # Per-thread try/except (matching partial_sync_inbox/extend_inbox_history below):
    # one flaky threads.get() must not 500 the whole bootstrap/recovery sync. Failed
    # stub ids are remembered so the reconcile step below can tell "gmail didn't list
    # it" apart from "we couldn't fetch it this round" — see failed_gmail_ids use.
    parsed_list: list[ParsedThread] = []
    failed_gmail_ids: set[str] = set()
    for stub in thread_stubs:
        tid = stub["id"]
        log.info("full_sync_inbox: fetching thread %s for user=%s", tid, user.id)
        try:
            thread_resp = gmail.users().threads().get(userId="me", id=tid, format="full").execute()
        except Exception:
            log.exception("full_sync_inbox: threads.get failed for %s; skipping", tid)
            failed_gmail_ids.add(tid)
            continue
        parsed_list.append(assemble_thread(thread_id=tid, raw_messages=thread_resp.get("messages", []) or []))

    bucket_ids = _classify_batch(db, user_id=user.id, parsed_list=parsed_list)
    internal_ids = [
        _upsert_thread_with_messages(db, user_id=user.id, parsed=p, bucket_id=b)
        for p, b in zip(parsed_list, bucket_ids)
    ]

    # Reconcile: full sync's labelIds=["INBOX"] listing is authoritative in
    # BOTH directions within the window it just observed — a thread it lists
    # IS in the inbox right now (even if a stale is_archived=True flag says
    # otherwise), and a stored, non-archived thread whose activity falls
    # inside the window but which the listing no longer returns has left the
    # inbox while our cursor was dead. Never delete rows either way; task
    # evidence may reference them.
    if parsed_list:
        listed_gmail_ids = {p.gmail_thread_id for p in parsed_list}

        # Un-archive: nothing else in the sync path ever clears is_archived,
        # so a thread that reappears in an INBOX listing must be cleared here.
        # Its internal id is already in internal_ids via the upsert loop
        # above (every parsed thread — listed thread — was upserted there),
        # so no extra appends are needed.
        reappeared = db.execute(
            select(InboxThread).where(
                InboxThread.user_id == user.id,
                InboxThread.gmail_id.in_(listed_gmail_ids),
                InboxThread.is_archived == True,  # noqa: E712
            )
        ).scalars().all()
        for t in reappeared:
            t.is_archived = False

        # Archive: only trust the window's floor when at least one listed
        # thread has a real timestamp. parser.assemble_thread returns
        # recent_internal_date=0 for a thread whose raw_messages came back
        # empty (a malformed/edge-case listing entry); if that 0 leaked into
        # window_min, the filter below (last_activity_at >= window_min) would
        # become last_activity_at >= 0 and match nearly every stored thread,
        # mass-archiving them in one sync. listed_gmail_ids above is still
        # built from ALL parsed threads (including messageless ones), so a
        # messageless listed thread itself is still never archived.
        window_dates = [p.recent_internal_date for p in parsed_list if p.recent_internal_date > 0]
        if window_dates:
            window_min = min(window_dates)
            # Exclude failed_gmail_ids alongside listed_gmail_ids: a threads.get()
            # failure means "we don't know this thread's current state this round,"
            # not "gmail no longer lists it." Without this, a transient per-thread
            # fetch error (see the try/except above) would make a still-live, merely
            # unfetchable thread look identical to one that actually left the inbox,
            # and the reconcile step below would wrongly archive it.
            stale = db.execute(
                select(InboxThread).where(
                    InboxThread.user_id == user.id,
                    InboxThread.is_archived == False,  # noqa: E712
                    InboxThread.gmail_id.not_in(listed_gmail_ids | failed_gmail_ids),
                    InboxThread.last_activity_at >= window_min,
                )
            ).scalars().all()
            for t in stale:
                t.is_archived = True
                internal_ids.append(t.id)
            if stale:
                log.info("full_sync_inbox: user=%s archived %d threads absent from listing",
                         user.id, len(stale))

    # Walk parsed_list once after upserting to find the max history_id across all
    # ingested messages — used to advance the user's gmail cursor.
    max_history_id: int = 0
    for parsed in parsed_list:
        for m in parsed.messages:
            try:
                hid = int(m.gmail_history_id)
            except (TypeError, ValueError):
                continue
            if hid > max_history_id:
                max_history_id = hid

    if max_history_id:
        inbox_repo.update_user_history_id(db, user_id=user.id, history_id=str(max_history_id))
    db.commit()
    log.info(
        "full_sync_inbox: user=%s done, %d threads touched, max_history_id=%d",
        user.id, len(internal_ids), max_history_id,
    )
    return internal_ids


def extend_inbox_history(db: Session, *, user: User, before_internal_date_ms: int) -> tuple[list[str], bool]:
    """Pull threads older than the given gmail_internal_date_ms. Returns
    (internal_thread_ids, more). more = (gmail returned 200 stubs).
    Caller manages the surrounding sync_lock. Does NOT touch gmail_last_history_id
    or clear inbox rows."""
    log.info("extend: user=%s before_ms=%d", user.id, before_internal_date_ms)
    gmail = get_gmail_client(db, user)
    before_secs = before_internal_date_ms // 1000
    listing = gmail.users().threads().list(
        userId="me", q=f"before:{before_secs}", maxResults=200, labelIds=["INBOX"],
    ).execute()
    stubs = listing.get("threads", []) or []

    parsed_list: list[ParsedThread] = []
    for stub in stubs:
        tid = stub["id"]
        try:
            resp = gmail.users().threads().get(userId="me", id=tid, format="full").execute()
            parsed_list.append(assemble_thread(thread_id=tid,
                                                raw_messages=resp.get("messages", []) or []))
        except Exception:
            log.exception("extend: threads.get failed for %s", tid)

    bucket_ids = _classify_batch(db, user_id=user.id, parsed_list=parsed_list)
    internal_ids = [
        _upsert_thread_with_messages(db, user_id=user.id, parsed=p, bucket_id=b)
        for p, b in zip(parsed_list, bucket_ids)
    ]
    db.commit()
    return internal_ids, len(stubs) == 200
