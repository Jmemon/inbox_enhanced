"""Task 7: the decoupled extraction pipeline (`app.task_engine.engine.
extract_for_pair` + the Celery tasks in `app.workers.task_engine_tasks`).

Uses eager celery + a file-backed sqlite session_factory (shared across the
two `.apply()` calls a "re-run" test makes) + fakeredis + a monkeypatched
`llm_client.call_messages` returning canned extraction JSON. `_publish` is
captured via `app.workers.tasks._publish` (task_engine_tasks late-imports it
at call time to avoid the tasks<->task_engine_tasks import cycle — see that
module's docstring — so patching the attribute on `app.workers.tasks` is what
the late import actually picks up).

Task 9's `backfill_task` tests are appended at the bottom of this file
(rather than a new `test_backfill.py`) since they reuse every fixture above
verbatim — the only new machinery is `_seed_user`/`_seed_backfill_thread`
(multiple threads under one user, explicit `last_activity_at`) and
`_backfill_llm_fake` (one `call_messages` fake that branches on
`kwargs["stage"]` to serve either a canned triage or extraction response,
since backfill_task's triage phase and extraction phase both funnel through
that same patched call point).
"""

import os
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, InboxMessage, InboxThread, User
from app.llm import client as llm_client
from app.task_engine import repo as task_repo
from app.task_engine.schema import AttributeSpec, EntitySpec, PipelineSpec, TaskStateSchema
from app.workers import task_engine_tasks

USER_ID = "u1"
THREAD_ID = "t1"
GMAIL_THREAD_ID = "gT1"
MESSAGE_GMAIL_ID = "gM1"
EVIDENCE_TEXT = "Your application has moved to in_progress review."


def _singleton_schema() -> TaskStateSchema:
    return TaskStateSchema(
        version=1, entity=None,
        pipeline=PipelineSpec(stages=["todo", "in_progress"], terminal=["done"]),
    )


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


@pytest.fixture(autouse=True)
def _wire_session_local(session_factory, monkeypatch):
    """process_task_updates/extract_for_thread each open their own session
    via the module-level SessionLocal seam — rebind it onto the test's
    in-memory-per-file engine, matching workers/tasks.py's test convention."""
    monkeypatch.setattr("app.workers.task_engine_tasks.SessionLocal", session_factory)


@pytest.fixture(autouse=True)
def _reset_llm_loop():
    llm_client.reset_for_tests()
    yield
    llm_client.reset_for_tests()


def _capture_publish(monkeypatch) -> list:
    captured: list[tuple[str, str, dict]] = []

    def _fake(user_id, event, payload):
        captured.append((user_id, event, payload))

    monkeypatch.setattr("app.workers.tasks._publish", _fake)
    return captured


def _seed_user_thread_message(
    session_factory, *, user_id=USER_ID, thread_id=THREAD_ID,
    gmail_thread_id=GMAIL_THREAD_ID, message_gmail_id=MESSAGE_GMAIL_ID,
    body_text=EVIDENCE_TEXT, internal_date=1_000_000,
):
    db = session_factory()
    db.add(User(id=user_id, email=f"{user_id}@x.com", created_at=datetime.now(timezone.utc)))
    db.add(InboxThread(id=thread_id, user_id=user_id, gmail_id=gmail_thread_id,
                       subject="Application status", bucket_id=None, recent_message_id=None))
    db.add(InboxMessage(
        id=f"im-{message_gmail_id}", thread_id=thread_id, user_id=user_id,
        gmail_id=message_gmail_id, gmail_thread_id=gmail_thread_id,
        gmail_internal_date=internal_date, gmail_history_id="h1",
        to_addr="me@x.com", from_addr="hr@corp.example",
        body_preview=body_text[:150], body_text=body_text,
    ))
    db.commit()
    db.close()


def _seed_tracker(session_factory, *, user_id=USER_ID, status="active", state_schema=None):
    """Create a tracker task. `state_schema` defaults to the valid singleton
    schema; pass a deliberately-broken dict (e.g. `{"garbage": True}`) to
    seed a tracker that will blow up in `schema.validate_schema` at
    extraction time — used by the batch-isolation regression test below."""
    schema = state_schema if state_schema is not None else _singleton_schema().model_dump()
    db = session_factory()
    task = task_repo.create_task(
        db, user_id=user_id, name="Job tracker", goal="Land the job",
        criteria="", state_schema=schema, kind="tracker",
    )
    task.status = status
    db.commit()
    task_id = task.id
    db.close()
    return task_id


def _attach(session_factory, *, task_id, thread_id, user_id=USER_ID, state="attached"):
    db = session_factory()
    task_repo.upsert_link(db, task_id=task_id, thread_id=thread_id, user_id=user_id,
                          origin="llm", state=state)
    db.commit()
    db.close()


def _extraction_response(*, field="stage", new_value="in_progress", confidence, message_id=MESSAGE_GMAIL_ID,
                         evidence="moved to in_progress review", entity="_self") -> str:
    return json.dumps([{
        "entity": entity, "is_new_entity": False, "field": field, "new_value": new_value,
        "evidence_quote": evidence, "message_id": message_id, "confidence": confidence,
    }])


def _fake_call_messages(response_text: str):
    async def _fake(**kwargs):
        return response_text
    return _fake


# ---------------------------------------------------------------------------
# End-to-end: applied event, board entity, version bump, ONE publish
# ---------------------------------------------------------------------------


def test_process_task_updates_end_to_end_applies_event_updates_board_and_publishes_once(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory)
    _attach(session_factory, task_id=task_id, thread_id=THREAD_ID)
    captured = _capture_publish(monkeypatch)

    settings = get_settings()
    high_confidence = min(100, settings.task_apply_confidence + 10)
    monkeypatch.setattr(
        llm_client, "call_messages",
        _fake_call_messages(_extraction_response(confidence=high_confidence)),
    )

    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])

    db = session_factory()
    events = task_repo.list_events(db, task_id=task_id, status="applied")
    assert len(events) == 1
    assert events[0].new_value == "in_progress"
    assert events[0].gmail_message_id == MESSAGE_GMAIL_ID

    entities = task_repo.list_entities(db, task_id=task_id)
    assert len(entities) == 1 and entities[0].entity_key == "_self"
    assert entities[0].state["stage"] == "in_progress"

    task = task_repo.get_owned_task(db, user_id=USER_ID, task_id=task_id)
    assert task.version == 2  # bumped once by the one applied event

    assert len(captured) == 1
    user_id, event, payload = captured[0]
    assert user_id == USER_ID
    assert event == "task_updated"
    assert payload == {"task_id": task_id, "version": 2, "pending_count": 0}


# ---------------------------------------------------------------------------
# Detached link: excluded from the (attached ∩ touched) intersection
# ---------------------------------------------------------------------------


def test_process_task_updates_skips_detached_link(session_factory, fake_redis, monkeypatch):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory)
    _attach(session_factory, task_id=task_id, thread_id=THREAD_ID, state="detached")
    captured = _capture_publish(monkeypatch)

    call_count = 0

    async def _fake(**kwargs):
        nonlocal call_count
        call_count += 1
        return _extraction_response(confidence=90)

    monkeypatch.setattr(llm_client, "call_messages", _fake)

    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])

    assert call_count == 0  # extraction never ran for a detached link
    assert captured == []  # nothing to report

    db = session_factory()
    assert task_repo.list_events(db, task_id=task_id) == []
    assert task_repo.list_entities(db, task_id=task_id) == []


# ---------------------------------------------------------------------------
# Paused task: excluded by list_active_trackers, never reaches extraction
# ---------------------------------------------------------------------------


def test_process_task_updates_skips_paused_task(session_factory, fake_redis, monkeypatch):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory, status="paused")
    _attach(session_factory, task_id=task_id, thread_id=THREAD_ID)
    captured = _capture_publish(monkeypatch)

    call_count = 0

    async def _fake(**kwargs):
        nonlocal call_count
        call_count += 1
        return _extraction_response(confidence=90)

    monkeypatch.setattr(llm_client, "call_messages", _fake)

    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])

    assert call_count == 0
    assert captured == []

    db = session_factory()
    assert task_repo.list_events(db, task_id=task_id) == []


# ---------------------------------------------------------------------------
# Low confidence -> pending_review + pending_count in the publish payload
# ---------------------------------------------------------------------------


def test_process_task_updates_low_confidence_stages_pending_with_pending_count(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory)
    _attach(session_factory, task_id=task_id, thread_id=THREAD_ID)
    captured = _capture_publish(monkeypatch)

    settings = get_settings()
    low_confidence = max(0, settings.task_apply_confidence - 20)
    monkeypatch.setattr(
        llm_client, "call_messages",
        _fake_call_messages(_extraction_response(confidence=low_confidence)),
    )

    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])

    db = session_factory()
    events = task_repo.list_events(db, task_id=task_id)
    assert len(events) == 1 and events[0].status == "pending_review"
    assert task_repo.pending_count(db, task_id=task_id) == 1

    task = task_repo.get_owned_task(db, user_id=USER_ID, task_id=task_id)
    assert task.version == 1  # pending_review never bumps version

    # Still one publish (pending_count changed even though version didn't).
    assert len(captured) == 1
    _, event, payload = captured[0]
    assert event == "task_updated"
    assert payload == {"task_id": task_id, "version": 1, "pending_count": 1}


# ---------------------------------------------------------------------------
# Re-run idempotency: no duplicate events, no noise publish
# ---------------------------------------------------------------------------


def test_process_task_updates_rerun_is_idempotent_no_dup_events_no_noise_publish(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory)
    _attach(session_factory, task_id=task_id, thread_id=THREAD_ID)
    captured = _capture_publish(monkeypatch)

    settings = get_settings()
    high_confidence = min(100, settings.task_apply_confidence + 10)
    monkeypatch.setattr(
        llm_client, "call_messages",
        _fake_call_messages(_extraction_response(confidence=high_confidence)),
    )

    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])
    assert len(captured) == 1
    captured.clear()

    db = session_factory()
    events_after_first = task_repo.list_events(db, task_id=task_id)
    assert len(events_after_first) == 1
    version_after_first = task_repo.get_owned_task(db, user_id=USER_ID, task_id=task_id).version

    # Re-run with the exact same input: the validator's idempotency check
    # (step 7) must drop the duplicate proposal — no new event, no version
    # bump, and (per this task's skip rule) no second publish.
    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])

    db2 = session_factory()
    events_after_second = task_repo.list_events(db2, task_id=task_id)
    assert len(events_after_second) == 1  # still just the one event, no duplicate
    task_after_second = task_repo.get_owned_task(db2, user_id=USER_ID, task_id=task_id)
    assert task_after_second.version == version_after_first  # unchanged

    assert captured == []  # no noise publish on the idempotent re-run


# ---------------------------------------------------------------------------
# Batch isolation: one corrupted tracker must not starve its siblings
# ---------------------------------------------------------------------------


def test_process_task_updates_isolates_task_with_corrupted_schema(
    session_factory, fake_redis, monkeypatch,
):
    """Reviewer repro: extract_for_pair's `validate_schema(task.state_schema)`
    call raises uncaught for a tracker with a corrupted schema. Without a
    per-task try/except, that exception propagates out of the `for task in
    list_active_trackers(...)` loop entirely — a healthy tracker scheduled
    after the corrupted one in the same run never even gets its LLM call.
    This asserts the healthy tracker's event still applies, its publish
    still fires, and no exception escapes `process_task_updates` itself."""
    _seed_user_thread_message(session_factory)

    bad_task_id = _seed_tracker(session_factory, state_schema={"garbage": True})
    healthy_task_id = _seed_tracker(session_factory)

    # list_active_trackers iterates in (created_at, id) order — pin the bad
    # tracker's created_at strictly before the healthy one's so the bad
    # tracker is guaranteed to be visited FIRST, which is what reproduces the
    # starvation bug (a later sibling never even gets its LLM call).
    db = session_factory()
    bad_task = task_repo.get_owned_task(db, user_id=USER_ID, task_id=bad_task_id)
    healthy_task = task_repo.get_owned_task(db, user_id=USER_ID, task_id=healthy_task_id)
    bad_task.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    healthy_task.created_at = datetime(2020, 1, 2, tzinfo=timezone.utc)
    db.commit()
    db.close()

    _attach(session_factory, task_id=bad_task_id, thread_id=THREAD_ID)
    _attach(session_factory, task_id=healthy_task_id, thread_id=THREAD_ID)
    captured = _capture_publish(monkeypatch)

    settings = get_settings()
    high_confidence = min(100, settings.task_apply_confidence + 10)
    call_count = 0

    async def _fake(**kwargs):
        nonlocal call_count
        call_count += 1
        return _extraction_response(confidence=high_confidence)

    monkeypatch.setattr(llm_client, "call_messages", _fake)

    result = task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])
    assert result.successful()  # the corrupted tracker's exception must not escape the task

    # Only the healthy tracker ever reached the LLM — the bad one blew up in
    # validate_schema before its extraction call.
    assert call_count == 1

    db = session_factory()
    healthy_events = task_repo.list_events(db, task_id=healthy_task_id, status="applied")
    assert len(healthy_events) == 1
    assert healthy_events[0].new_value == "in_progress"

    bad_events = task_repo.list_events(db, task_id=bad_task_id)
    assert bad_events == []  # corrupted tracker produced nothing, no partial writes

    assert len(captured) == 1  # only the healthy tracker's publish fired
    user_id, event, payload = captured[0]
    assert user_id == USER_ID
    assert event == "task_updated"
    assert payload["task_id"] == healthy_task_id


# ---------------------------------------------------------------------------
# extract_for_thread: single-pair variant (Task 10's future attach flow)
# ---------------------------------------------------------------------------


def test_extract_for_thread_applies_and_publishes(session_factory, fake_redis, monkeypatch):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory)
    # No pre-existing link needed — extract_for_thread is the single-pair
    # entrypoint invoked directly by the attach action itself.
    captured = _capture_publish(monkeypatch)

    settings = get_settings()
    high_confidence = min(100, settings.task_apply_confidence + 10)
    monkeypatch.setattr(
        llm_client, "call_messages",
        _fake_call_messages(_extraction_response(confidence=high_confidence)),
    )

    task_engine_tasks.extract_for_thread.apply(args=[USER_ID, task_id, THREAD_ID])

    db = session_factory()
    events = task_repo.list_events(db, task_id=task_id, status="applied")
    assert len(events) == 1
    assert len(captured) == 1
    _, event, payload = captured[0]
    assert event == "task_updated"
    assert payload["task_id"] == task_id and payload["pending_count"] == 0


def test_extract_for_thread_skips_paused_task(session_factory, fake_redis, monkeypatch):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory, status="paused")
    captured = _capture_publish(monkeypatch)

    call_count = 0

    async def _fake(**kwargs):
        nonlocal call_count
        call_count += 1
        return _extraction_response(confidence=90)

    monkeypatch.setattr(llm_client, "call_messages", _fake)

    task_engine_tasks.extract_for_thread.apply(args=[USER_ID, task_id, THREAD_ID])

    assert call_count == 0
    assert captured == []


def test_extract_for_thread_skips_non_tracker_kind(session_factory, fake_redis, monkeypatch):
    """Matches the batch path's implicit filter: list_active_trackers only
    ever selects kind='tracker' tasks, so this single-pair entrypoint must
    apply the same guard rather than happily extracting against a
    bucket-kind task."""
    _seed_user_thread_message(session_factory)
    db = session_factory()
    task = task_repo.create_task(
        db, user_id=USER_ID, name="Bucket", goal="", criteria="",
        state_schema=_singleton_schema().model_dump(), kind="bucket",
    )
    task_id = task.id
    db.commit()
    db.close()
    captured = _capture_publish(monkeypatch)

    call_count = 0

    async def _fake(**kwargs):
        nonlocal call_count
        call_count += 1
        return _extraction_response(confidence=90)

    monkeypatch.setattr(llm_client, "call_messages", _fake)

    task_engine_tasks.extract_for_thread.apply(args=[USER_ID, task_id, THREAD_ID])

    assert call_count == 0
    assert captured == []


# ---------------------------------------------------------------------------
# Task 9: backfill_task
# ---------------------------------------------------------------------------


def _seed_user(session_factory, *, user_id=USER_ID):
    db = session_factory()
    db.add(User(id=user_id, email=f"{user_id}@x.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    db.close()


def _seed_backfill_thread(
    session_factory, *, user_id=USER_ID, thread_id, gmail_thread_id, message_gmail_id,
    body_text, last_activity_at, is_archived=False,
):
    """Seed one thread + its one message for backfill tests.

    Unlike `_seed_user_thread_message` (which also creates the User row),
    this assumes the user already exists -- backfill tests seed several
    threads under one shared user via `_seed_user` -- and sets
    `last_activity_at` explicitly, since the ascending-extraction-order test
    needs distinct, controlled values (recall `_seed_user_thread_message`
    never sets it at all)."""
    db = session_factory()
    db.add(InboxThread(
        id=thread_id, user_id=user_id, gmail_id=gmail_thread_id, subject="Re: application",
        bucket_id=None, recent_message_id=None, is_archived=is_archived,
        last_activity_at=last_activity_at,
    ))
    db.add(InboxMessage(
        id=f"im-{message_gmail_id}", thread_id=thread_id, user_id=user_id,
        gmail_id=message_gmail_id, gmail_thread_id=gmail_thread_id,
        gmail_internal_date=last_activity_at, gmail_history_id="h1",
        to_addr="me@x.com", from_addr="hr@corp.example",
        body_preview=body_text[:150], body_text=body_text,
    ))
    db.commit()
    db.close()


def _backfill_llm_fake(*, tracker_name, marker_confidences, marker_extractions):
    """One `call_messages` fake covering BOTH of backfill_task's LLM call
    shapes -- classify.triage()'s per-candidate relevance call (stage=
    "classify") and engine.extract_for_pair's per-thread extraction call
    (stage="extract") -- since both funnel through the same patched
    `llm_client.call_messages` attribute.

    Threads are told apart by a marker substring embedded in their
    body_text, which both `thread_to_string` (triage) and
    `thread_to_string_with_ids` (extraction) embed verbatim into the
    rendered user message. `marker_confidences` maps marker -> confidence
    (None means "no relevant_tasks entry at all", i.e. a clean non-match).
    `marker_extractions` maps marker -> the canned extraction JSON string
    returned for that thread's extract call (threads not present there fall
    back to "[]", i.e. no proposals)."""
    async def _fake(**kwargs):
        stage = kwargs.get("stage")
        user_msg = kwargs.get("user", "")
        if stage == "classify":
            for marker, confidence in marker_confidences.items():
                if marker in user_msg:
                    if confidence is None:
                        return json.dumps({"bucket_name": None, "relevant_tasks": []})
                    return json.dumps({
                        "bucket_name": None,
                        "relevant_tasks": [{"name": tracker_name, "confidence": confidence}],
                    })
            return json.dumps({"bucket_name": None, "relevant_tasks": []})
        if stage == "extract":
            for marker, response in marker_extractions.items():
                if marker in user_msg:
                    return response
            return "[]"
        return "[]"
    return _fake


def _multi_entity_schema_dict() -> dict:
    """Non-singleton schema (named 'company' entities with a freeform string
    attribute) -- used by the ascending-order test below because plain
    'stage' transitions are validator-guaranteed to converge to whichever
    target is furthest along the pipeline REGARDLESS of processing order
    (forward moves always apply, backward moves always defer to
    pending_review -- see transitions.py step 3), which makes 'stage' useless
    for proving chronological processing order. A plain attribute field has
    no such ordering guard: whichever proposal is applied LAST simply wins,
    so it's the only field kind that can actually distinguish "processed
    ascending" from "processed in the wrong order."""
    return TaskStateSchema(
        version=1,
        entity=EntitySpec(noun="company", attributes=[AttributeSpec(key="status_note", type="string")]),
        pipeline=PipelineSpec(stages=["todo"], terminal=["done"]),
    ).model_dump()


def test_backfill_task_links_matches_and_extracts_ascending_final_state_reflects_later_message(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    task_id = _seed_tracker(session_factory, state_schema=_multi_entity_schema_dict())

    # THREAD_A is older (last_activity_at=1_000_000), THREAD_B is newer
    # (2_000_000) -- extraction must run A then B for B's value to win.
    _seed_backfill_thread(
        session_factory, thread_id="bfA", gmail_thread_id="gbfA", message_gmail_id="mA",
        body_text="MARKER-A Acme Corp status update: applied for the role.",
        last_activity_at=1_000_000,
    )
    _seed_backfill_thread(
        session_factory, thread_id="bfB", gmail_thread_id="gbfB", message_gmail_id="mB",
        body_text="MARKER-B Acme Corp status update: interview scheduled next week.",
        last_activity_at=2_000_000,
    )

    fake = _backfill_llm_fake(
        tracker_name="Job tracker",
        marker_confidences={"MARKER-A": 90, "MARKER-B": 90},
        marker_extractions={
            "MARKER-A": json.dumps([{
                "entity": "Acme Corp", "is_new_entity": True, "field": "status_note",
                "new_value": "Applied", "evidence_quote": "applied for the role",
                "message_id": "mA", "confidence": 90,
            }]),
            "MARKER-B": json.dumps([{
                "entity": "Acme Corp", "is_new_entity": True, "field": "status_note",
                "new_value": "Interview scheduled", "evidence_quote": "interview scheduled next week",
                "message_id": "mB", "confidence": 90,
            }]),
        },
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None])

    db = session_factory()
    link_a = task_repo.get_link(db, task_id=task_id, thread_id="bfA")
    link_b = task_repo.get_link(db, task_id=task_id, thread_id="bfB")
    assert link_a is not None and link_a.state == "attached" and link_a.origin == "llm"
    assert link_b is not None and link_b.state == "attached" and link_b.origin == "llm"

    entities = task_repo.list_entities(db, task_id=task_id)
    assert len(entities) == 1  # both proposals resolved to the same "Acme Corp" entity
    # Ascending (A then B) means B's proposal is applied LAST -- if backfill
    # extracted in the wrong order, this would read "Applied" (A's value)
    # instead.
    assert entities[0].state["status_note"] == "Interview scheduled"

    events = task_repo.list_events(db, task_id=task_id, status="applied")
    assert len(events) == 2  # both applied -- plain attribute field, no stage-ordering guard


def test_backfill_task_publishes_progress_every_interval_and_terminal_done(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    task_id = _seed_tracker(session_factory)  # default singleton schema, name "Job tracker"

    # Small batch/interval so 4 seeded threads cross the progress cadence
    # twice without needing 50+ real rows.
    monkeypatch.setattr(task_engine_tasks, "BACKFILL_TRIAGE_BATCH", 2)
    monkeypatch.setattr(task_engine_tasks, "BACKFILL_PROGRESS_INTERVAL", 2)

    # Descending last_activity_at so list_threads' recency order is
    # deterministic: candidate batches are [M1, N1] then [M2, N2].
    _seed_backfill_thread(session_factory, thread_id="m1", gmail_thread_id="gm1",
                          message_gmail_id="mm1", body_text="MARKER-M1 relevant thread.",
                          last_activity_at=4000)
    _seed_backfill_thread(session_factory, thread_id="n1", gmail_thread_id="gn1",
                          message_gmail_id="mn1", body_text="MARKER-N1 unrelated thread.",
                          last_activity_at=3000)
    _seed_backfill_thread(session_factory, thread_id="m2", gmail_thread_id="gm2",
                          message_gmail_id="mm2", body_text="MARKER-M2 relevant thread.",
                          last_activity_at=2000)
    _seed_backfill_thread(session_factory, thread_id="n2", gmail_thread_id="gn2",
                          message_gmail_id="mn2", body_text="MARKER-N2 unrelated thread.",
                          last_activity_at=1000)

    fake = _backfill_llm_fake(
        tracker_name="Job tracker",
        marker_confidences={
            "MARKER-M1": 90, "MARKER-M2": 90, "MARKER-N1": None, "MARKER-N2": None,
        },
        marker_extractions={},  # extraction phase is a no-op -- only progress matters here
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None])

    progress = [(e, p) for _, e, p in captured if e == "task_backfill_progress"]
    updated = [(e, p) for _, e, p in captured if e == "task_updated"]

    assert [p for _, p in progress] == [
        {"task_id": task_id, "scanned": 2, "matched": 1, "done": False},
        {"task_id": task_id, "scanned": 4, "matched": 2, "done": False},
        {"task_id": task_id, "scanned": 4, "matched": 2, "done": True},
    ]
    assert len(updated) == 1  # exactly one final task_updated, after the terminal progress
    assert captured[-1][1] == "task_updated"  # task_updated is always last


def test_backfill_task_rerun_is_idempotent_and_publish_counts_match(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    task_id = _seed_tracker(session_factory)
    _seed_backfill_thread(
        session_factory, thread_id="bf1", gmail_thread_id="g1", message_gmail_id="m1",
        body_text="MARKER-1 evidence: moved to in_progress review.", last_activity_at=1000,
    )

    settings = get_settings()
    high_confidence = min(100, settings.task_apply_confidence + 10)
    fake = _backfill_llm_fake(
        tracker_name="Job tracker",
        marker_confidences={"MARKER-1": 90},
        marker_extractions={
            "MARKER-1": _extraction_response(
                confidence=high_confidence, message_id="m1",
                evidence="moved to in_progress review",
            ),
        },
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None])
    first_publishes = list(captured)
    captured.clear()

    db = session_factory()
    events_after_first = task_repo.list_events(db, task_id=task_id)
    assert len(events_after_first) == 1
    links_after_first = task_repo.list_attached_thread_ids(db, task_id=task_id)
    assert links_after_first == {"bf1"}

    # Re-run with the exact same input.
    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None])
    second_publishes = list(captured)

    db2 = session_factory()
    events_after_second = task_repo.list_events(db2, task_id=task_id)
    assert len(events_after_second) == 1  # no duplicate event
    links_after_second = task_repo.list_attached_thread_ids(db2, task_id=task_id)
    assert links_after_second == {"bf1"}  # no duplicate link row either

    # Backfill is a one-shot job the wizard waits on -- it always reports a
    # definitive completion, so BOTH runs publish the same shape (unlike
    # process_task_updates' skip-on-no-change suppression for a sync tick).
    assert [e for _, e, _ in first_publishes] == [e for _, e, _ in second_publishes] == [
        "task_backfill_progress", "task_updated",
    ]
    assert first_publishes[0][2] == second_publishes[0][2] == {
        "task_id": task_id, "scanned": 1, "matched": 1, "done": True,
    }
    assert first_publishes[1][2] == second_publishes[1][2]  # same version/pending_count


def test_backfill_task_never_reattaches_user_detached_link(session_factory, fake_redis, monkeypatch):
    _seed_user(session_factory)
    task_id = _seed_tracker(session_factory)
    _seed_backfill_thread(
        session_factory, thread_id="bfD", gmail_thread_id="gD", message_gmail_id="mD",
        body_text="MARKER-D strong evidence this thread matches the tracker.",
        last_activity_at=500,
    )

    # User explicitly detached this thread from the tracker before backfill
    # ever ran (e.g. attached automatically once, then the user removed it).
    db = session_factory()
    task_repo.upsert_link(db, task_id=task_id, thread_id="bfD", user_id=USER_ID,
                          origin="user", state="detached")
    db.commit()
    db.close()

    fake = _backfill_llm_fake(
        tracker_name="Job tracker",
        marker_confidences={"MARKER-D": 95},  # high confidence -- would normally link
        marker_extractions={"MARKER-D": _extraction_response(confidence=90, message_id="mD")},
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None])

    db2 = session_factory()
    link = task_repo.get_link(db2, task_id=task_id, thread_id="bfD")
    assert link is not None
    assert link.origin == "user" and link.state == "detached"  # never re-attached

    assert task_repo.list_events(db2, task_id=task_id) == []  # never extracted either

    # Progress still reports scanned=1 but matched=0 -- the sticky rule
    # blocked the attach, so this thread never counted as matched.
    progress = [p for _, e, p in captured if e == "task_backfill_progress" and p["done"]]
    assert progress == [{"task_id": task_id, "scanned": 1, "matched": 0, "done": True}]
