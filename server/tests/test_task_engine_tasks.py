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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, InboxMessage, InboxThread, User
from app.llm import client as llm_client
from app.task_engine import jobs_repo
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
# Task 2 (spec §4.6 learning loop): recent user corrections thread into the
# extraction prompt via engine.extract_for_pair -> extract_transition.
# build_user_message
# ---------------------------------------------------------------------------


def test_extract_for_pair_threads_recent_user_corrections_into_prompt(
    session_factory, fake_redis, monkeypatch,
):
    """A prior manual correction (origin='user', status='applied') on this
    task's entity must show up, rendered, inside the actual `user` message
    handed to the extraction LLM call — proving engine.extract_for_pair
    fetches repo.recent_user_events and threads it through
    extract_transition.build_user_message, not just that the plumbing
    compiles."""
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory)
    _attach(session_factory, task_id=task_id, thread_id=THREAD_ID)

    db = session_factory()
    task = task_repo.get_owned_task(db, user_id=USER_ID, task_id=task_id)
    entity = task_repo.get_or_create_entity(
        db, task_id=task_id, user_id=USER_ID, entity_key="_self", display_name="Self",
    )
    db.commit()
    correction = task_repo.append_event(
        db, task=task, entity=entity, origin="user", status="applied",
        field="stage", old_value="todo", new_value="in_progress",
    )
    task_repo.apply_event(db, task=task, entity=entity, event=correction)
    db.commit()
    db.close()

    captured_kwargs: dict = {}

    async def _fake(**kwargs):
        captured_kwargs.update(kwargs)
        return _extraction_response(confidence=90)

    monkeypatch.setattr(llm_client, "call_messages", _fake)

    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])

    assert "Corrections the user has made (respect these):" in captured_kwargs["user"]
    assert '- Self: user set stage to "in_progress"' in captured_kwargs["user"]


def test_extract_for_pair_omits_corrections_section_with_no_prior_corrections(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user_thread_message(session_factory)
    task_id = _seed_tracker(session_factory)
    _attach(session_factory, task_id=task_id, thread_id=THREAD_ID)

    captured_kwargs: dict = {}

    async def _fake(**kwargs):
        captured_kwargs.update(kwargs)
        return _extraction_response(confidence=90)

    monkeypatch.setattr(llm_client, "call_messages", _fake)

    task_engine_tasks.process_task_updates.apply(args=[USER_ID, [THREAD_ID]])

    assert "Corrections the user has made" not in captured_kwargs["user"]


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


# ---------------------------------------------------------------------------
# Phase 4 Task 2: backfill_task's kind='bucket' branch -- a pure
# reclassification pass (no task_thread_links, no task_events, no
# extraction). Reuses every fixture above; only new machinery is
# _seed_bucket_task (a kind='bucket' task, state_schema=None like every real
# bucket) and _fake_triage_capturing (a classify.triage() stand-in that
# records each call's (buckets, trackers, current_bucket_ids) and resolves a
# per-thread bucket pick by a marker substring in that thread's body_text --
# _seed_backfill_thread hardcodes the same subject for every thread, so
# picks can't be keyed by subject the way _backfill_llm_fake keys by stage).
# ---------------------------------------------------------------------------


def _seed_bucket_task(session_factory, *, user_id=USER_ID, name="Bucket") -> str:
    db = session_factory()
    task = task_repo.create_task(
        db, user_id=user_id, name=name, goal="", criteria="bucket criteria",
        state_schema=None, kind="bucket",
    )
    db.commit()
    task_id = task.id
    db.close()
    return task_id


def _fake_triage_capturing(calls: list, picks_by_marker: dict):
    def _fake(threads, buckets, trackers, current_bucket_ids, *, user_id=None, task_id=None):
        calls.append({
            "buckets": list(buckets), "trackers": list(trackers),
            "current_bucket_ids": list(current_bucket_ids),
            "user_id": user_id, "task_id": task_id,
        })
        out = []
        for parsed, cur in zip(threads, current_bucket_ids):
            combined = " ".join(m.body_text for m in parsed.messages)
            pick = cur  # stability default: no marker match keeps the current pick
            for marker, bucket_id in picks_by_marker.items():
                if marker in combined:
                    pick = bucket_id
                    break
            out.append((pick, []))
        return out
    return _fake


def test_backfill_bucket_writes_changed_bucket_ids_and_skips_unchanged(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    bucket_id = _seed_bucket_task(session_factory, name="Receipts")

    _seed_backfill_thread(
        session_factory, thread_id="bkA", gmail_thread_id="gbkA", message_gmail_id="mA",
        body_text="MARKER-A receipt for your order.", last_activity_at=1000,
    )
    _seed_backfill_thread(
        session_factory, thread_id="bkB", gmail_thread_id="gbkB", message_gmail_id="mB",
        body_text="MARKER-B team meeting notes.", last_activity_at=2000,
    )
    db = session_factory()
    db.get(InboxThread, "bkA").bucket_id = "old-bucket"
    db.get(InboxThread, "bkB").bucket_id = "unchanged-bucket"
    db.commit()
    db.close()

    calls: list = []
    fake = _fake_triage_capturing(
        calls, picks_by_marker={"MARKER-A": bucket_id, "MARKER-B": "unchanged-bucket"},
    )
    monkeypatch.setattr("app.llm.classify.triage", fake)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, bucket_id, None])

    db2 = session_factory()
    assert db2.get(InboxThread, "bkA").bucket_id == bucket_id  # pick differed -> written
    assert db2.get(InboxThread, "bkB").bucket_id == "unchanged-bucket"  # pick matched -> untouched

    # Stability hint: triage's current_bucket_ids must carry each thread's
    # ACTUAL stored bucket_id at call time, not None/blank.
    all_current = [cur for call in calls for cur in call["current_bucket_ids"]]
    assert "old-bucket" in all_current
    assert "unchanged-bucket" in all_current

    for call in calls:
        assert call["trackers"] == []  # bucket branch never passes trackers
        assert call["task_id"] == bucket_id
        # FULL active bucket set, new bucket included -- not just this one.
        assert any(b.id == bucket_id for b in call["buckets"])


def test_backfill_bucket_never_writes_links_events_or_extracts(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    bucket_id = _seed_bucket_task(session_factory, name="Receipts")
    _seed_backfill_thread(
        session_factory, thread_id="bkC", gmail_thread_id="gbkC", message_gmail_id="mC",
        body_text="MARKER-C receipt.", last_activity_at=1000,
    )

    calls: list = []
    fake = _fake_triage_capturing(calls, picks_by_marker={"MARKER-C": bucket_id})
    monkeypatch.setattr("app.llm.classify.triage", fake)

    def _extract_should_not_run(*args, **kwargs):
        raise AssertionError("extract_for_pair must never run for a bucket-kind backfill")
    monkeypatch.setattr(task_engine_tasks, "extract_for_pair", _extract_should_not_run)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, bucket_id, None])

    db = session_factory()
    assert task_repo.list_attached_thread_ids(db, task_id=bucket_id) == set()
    assert task_repo.list_events(db, task_id=bucket_id) == []
    assert db.get(InboxThread, "bkC").bucket_id == bucket_id  # the reclassify itself still happened


def test_backfill_bucket_uses_recency_fallback_when_probes_empty(
    session_factory, fake_redis, monkeypatch,
):
    """A bucket backfill is always enqueued with keyword_probes=[] (see
    api/buckets.py's POST route) -- _backfill_candidate_pool's unconditional
    recency-window union must still surface every stored thread for the
    triage pass, same fallback the tracker branch relies on."""
    _seed_user(session_factory)
    bucket_id = _seed_bucket_task(session_factory, name="Receipts")
    _seed_backfill_thread(
        session_factory, thread_id="bkD1", gmail_thread_id="gbkD1", message_gmail_id="mD1",
        body_text="MARKER-D1 first thread.", last_activity_at=3000,
    )
    _seed_backfill_thread(
        session_factory, thread_id="bkD2", gmail_thread_id="gbkD2", message_gmail_id="mD2",
        body_text="MARKER-D2 second thread.", last_activity_at=2000,
    )

    calls: list = []
    fake = _fake_triage_capturing(calls, picks_by_marker={})  # no picks -> nothing ever changes
    monkeypatch.setattr("app.llm.classify.triage", fake)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, bucket_id, []])

    progress_done = [p for _, e, p in captured if e == "task_backfill_progress" and p["done"]]
    # scanned=2 proves BOTH recency-window threads were triaged despite empty
    # keyword_probes; matched=0 since no bucket pick ever changed.
    assert progress_done == [{"task_id": bucket_id, "scanned": 2, "matched": 0, "done": True}]


def test_backfill_bucket_publishes_threads_updated_then_terminal_progress_no_task_updated(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    bucket_id = _seed_bucket_task(session_factory, name="Receipts")
    _seed_backfill_thread(
        session_factory, thread_id="bkE", gmail_thread_id="gbkE", message_gmail_id="mE",
        body_text="MARKER-E receipt.", last_activity_at=1000,
    )
    db = session_factory()
    db.get(InboxThread, "bkE").bucket_id = "old-bucket"
    db.commit()
    db.close()

    calls: list = []
    fake = _fake_triage_capturing(calls, picks_by_marker={"MARKER-E": bucket_id})
    monkeypatch.setattr("app.llm.classify.triage", fake)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, bucket_id, None])

    events = [(e, p) for _, e, p in captured]
    assert events == [
        ("threads_updated", {"thread_ids": ["bkE"]}),
        ("task_backfill_progress", {"task_id": bucket_id, "scanned": 1, "matched": 1, "done": True}),
    ]
    # No final task_updated -- a bucket-kind task carries no task-engine
    # board/event state for a client to refetch (unlike the tracker branch).
    assert all(e != "task_updated" for e, _ in events)


def test_backfill_bucket_skips_stale_write_when_bucket_moves_during_triage(
    session_factory, fake_redis, monkeypatch,
):
    """Fix round 1 regression test for the optimistic write-time guard in
    `_run_bucket_backfill`: a concurrent poll (holding sync_lock, off in its
    own session) reclassifies a thread against FRESH content and commits a
    new bucket_id WHILE this batch's own `classify.triage()` call is still
    running -- after this batch already read the OLD bucket_id as its
    stability hint, but before this batch writes its own (now stale-content)
    pick. The backfill must not clobber the fresher value, and the thread
    must not appear in the `threads_updated` publish.

    The concurrent commit is simulated via a raw SQL UPDATE against the SAME
    session `backfill_task` uses (captured below by wrapping SessionLocal),
    deliberately bypassing the ORM so the already-loaded InboxThread entity
    (loaded earlier by `load_parsed_threads`, before this fake runs) is left
    with a stale `bucket_id` attribute in the session's identity map -- a
    real concurrent session would leave it exactly this stale, since
    `expire_on_commit` only ever invalidates a session's OWN identity map,
    never another session's. This is what proves the guard re-reads via a
    fresh column select rather than `db.get(...)`'s cached object: without
    the fix, `db.get(InboxThread, thread_id).bucket_id = new_bucket` would
    overwrite the fresher value regardless of what's actually stored.
    """
    _seed_user(session_factory)
    bucket_id = _seed_bucket_task(session_factory, name="Receipts")
    _seed_backfill_thread(
        session_factory, thread_id="bkF", gmail_thread_id="gbkF", message_gmail_id="mF",
        body_text="MARKER-F receipt.", last_activity_at=1000,
    )
    db = session_factory()
    db.get(InboxThread, "bkF").bucket_id = "old-bucket"
    db.commit()
    db.close()

    captured_session: dict = {}

    def _spy_session_local():
        s = session_factory()
        captured_session["db"] = s
        return s
    monkeypatch.setattr(task_engine_tasks, "SessionLocal", _spy_session_local)

    def _fake_triage_with_concurrent_write(threads, buckets, trackers, current_bucket_ids, *, user_id=None, task_id=None):
        # Simulate the concurrent poll's commit landing mid-triage, via a raw
        # UPDATE that bypasses the ORM (so the already-loaded "bkF" entity's
        # cached bucket_id attribute is left stale, matching what a genuinely
        # separate session would leave behind).
        captured_session["db"].execute(
            text("UPDATE inbox_threads SET bucket_id = :b WHERE id = :tid"),
            {"b": "fresher-bucket", "tid": "bkF"},
        )
        # This batch's own pick, computed from the stale content it read
        # before the concurrent write above -- deliberately a THIRD value so
        # a wrongly-applied write is unambiguous below.
        return [("stale-pick-bucket", [])]
    monkeypatch.setattr("app.llm.classify.triage", _fake_triage_with_concurrent_write)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, bucket_id, None])

    db2 = session_factory()
    # The concurrent write wins -- the backfill's stale-content pick must
    # never land.
    assert db2.get(InboxThread, "bkF").bucket_id == "fresher-bucket"

    # The thread must be absent from every threads_updated publish (the
    # guard excludes it from changed_thread_ids entirely).
    updated_ids = [
        tid for _, e, p in captured if e == "threads_updated" for tid in p["thread_ids"]
    ]
    assert "bkF" not in updated_ids


# ---------------------------------------------------------------------------
# Phase 4.5 Task 2: backfill_task threads an optional job_id through to
# jobs_repo -- per-batch progress writes + a terminal 'done'/'failed' stage,
# and a top-level try/except that marks the job failed (via a FRESH session)
# and re-raises on any exception. job_id=None (every pre-existing caller)
# must take none of these extra reads/writes at all.
# ---------------------------------------------------------------------------


def _seed_job(session_factory, *, user_id=USER_ID, task_kind="tracker") -> str:
    db = session_factory()
    job = jobs_repo.create_job(db, user_id=user_id, kind="creation", task_kind=task_kind, goal="g")
    db.commit()
    job_id = job.id
    db.close()
    return job_id


def test_backfill_task_job_id_none_never_touches_jobs_repo(session_factory, fake_redis, monkeypatch):
    """The default (and every pre-existing caller's) path must be byte-
    identical: no job row reads or writes at all when job_id is omitted."""
    _seed_user(session_factory)
    task_id = _seed_tracker(session_factory)
    _seed_backfill_thread(
        session_factory, thread_id="bfN", gmail_thread_id="gN", message_gmail_id="mN",
        body_text="MARKER-N no job wired up.", last_activity_at=1000,
    )

    def _boom(*args, **kwargs):
        raise AssertionError("jobs_repo must not be touched on the job_id=None path")

    for name in ("get_owned_job", "update_progress", "update_stage", "mark_failed"):
        monkeypatch.setattr(jobs_repo, name, _boom)

    fake = _backfill_llm_fake(
        tracker_name="Job tracker", marker_confidences={"MARKER-N": 90},
        marker_extractions={"MARKER-N": _extraction_response(confidence=90, message_id="mN")},
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)

    # Must complete without raising -- if any jobs_repo call happened, _boom
    # above would blow up first (and Celery-eager would propagate it).
    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None])


def test_backfill_task_tracker_with_job_id_writes_progress_per_batch_and_terminal_done(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    task_id = _seed_tracker(session_factory)
    job_id = _seed_job(session_factory, task_kind="tracker")

    # Small batch/interval, mirroring the non-job progress-cadence test above.
    monkeypatch.setattr(task_engine_tasks, "BACKFILL_TRIAGE_BATCH", 2)
    monkeypatch.setattr(task_engine_tasks, "BACKFILL_PROGRESS_INTERVAL", 2)

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

    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None, job_id])

    job_updates = [p for _, e, p in captured if e == "job_updated"]
    # One per triage batch (2 batches of 2) + one terminal 'done' write == 3.
    # Every payload is the bare {"job_id": ...} -- events carry ids, never rows.
    assert job_updates == [{"job_id": job_id}] * 3

    db = session_factory()
    job = jobs_repo.get_owned_job(db, user_id=USER_ID, job_id=job_id)
    assert job.stage == "done"
    assert job.scanned == 4
    assert job.matched == 2
    assert job.total == 4  # candidate-pool size, constant across every write


def test_backfill_bucket_with_job_id_writes_progress_and_terminal_done(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    bucket_id = _seed_bucket_task(session_factory, name="Receipts")
    job_id = _seed_job(session_factory, task_kind="bucket")
    _seed_backfill_thread(
        session_factory, thread_id="bkJ", gmail_thread_id="gbkJ", message_gmail_id="mJ",
        body_text="MARKER-J receipt.", last_activity_at=1000,
    )
    db = session_factory()
    db.get(InboxThread, "bkJ").bucket_id = "old-bucket"
    db.commit()
    db.close()

    calls: list = []
    fake = _fake_triage_capturing(calls, picks_by_marker={"MARKER-J": bucket_id})
    monkeypatch.setattr("app.llm.classify.triage", fake)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, bucket_id, None, job_id])

    job_updates = [p for _, e, p in captured if e == "job_updated"]
    assert job_updates == [{"job_id": job_id}] * 2  # one batch + terminal done

    db2 = session_factory()
    job = jobs_repo.get_owned_job(db2, user_id=USER_ID, job_id=job_id)
    assert job.stage == "done"
    assert job.scanned == 1
    assert job.matched == 1
    assert job.total == 1


def test_backfill_task_exception_marks_job_failed_with_error_and_reraises(
    session_factory, fake_redis, monkeypatch,
):
    """Top-level try/except: the SECOND triage batch raises mid-run (after
    the first batch's progress already committed) -- the job row must end up
    'failed' with the exception text, the FIRST batch's progress must
    survive (not rolled back away), and the exception must still propagate
    so Celery records the run as FAILED (not silently swallowed)."""
    _seed_user(session_factory)
    bucket_id = _seed_bucket_task(session_factory, name="Receipts")
    job_id = _seed_job(session_factory, task_kind="bucket")

    monkeypatch.setattr(task_engine_tasks, "BACKFILL_TRIAGE_BATCH", 1)

    _seed_backfill_thread(
        session_factory, thread_id="bkOK", gmail_thread_id="gOK", message_gmail_id="mOK",
        body_text="MARKER-OK first, succeeds.", last_activity_at=2000,
    )
    _seed_backfill_thread(
        session_factory, thread_id="bkBoom", gmail_thread_id="gBoom", message_gmail_id="mBoom",
        body_text="MARKER-BOOM second, blows up.", last_activity_at=1000,
    )

    call_count = 0

    def _fake_triage_then_boom(threads, buckets, trackers, current_bucket_ids, *, user_id=None, task_id=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [(cur, []) for cur in current_bucket_ids]  # first batch: no-op pick
        raise RuntimeError("triage blew up on batch 2")

    monkeypatch.setattr("app.llm.classify.triage", _fake_triage_then_boom)
    captured = _capture_publish(monkeypatch)

    with pytest.raises(RuntimeError, match="triage blew up on batch 2"):
        task_engine_tasks.backfill_task.apply(args=[USER_ID, bucket_id, None, job_id])

    db = session_factory()
    job = jobs_repo.get_owned_job(db, user_id=USER_ID, job_id=job_id)
    assert job.stage == "failed"
    assert "triage blew up on batch 2" in job.error
    assert job.scanned == 1  # the first (successful) batch's progress survives
    assert job.total == 2

    job_updates = [p for _, e, p in captured if e == "job_updated"]
    assert job_updates[-1] == {"job_id": job_id}  # the failure write's own nudge
    assert len(job_updates) == 2  # batch-1 progress + the failure write


def test_backfill_task_missing_job_row_is_a_defensive_noop(session_factory, fake_redis, monkeypatch):
    """job_id pointing at a job row that doesn't exist (or isn't owned by
    this user) must not crash the backfill -- it just skips every job write,
    same as if job_id were never given."""
    _seed_user(session_factory)
    task_id = _seed_tracker(session_factory)
    _seed_backfill_thread(
        session_factory, thread_id="bfG", gmail_thread_id="gG", message_gmail_id="mG",
        body_text="MARKER-G ghost job id.", last_activity_at=1000,
    )
    fake = _backfill_llm_fake(
        tracker_name="Job tracker", marker_confidences={"MARKER-G": 90},
        marker_extractions={"MARKER-G": _extraction_response(confidence=90, message_id="mG")},
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)
    captured = _capture_publish(monkeypatch)

    task_engine_tasks.backfill_task.apply(args=[USER_ID, task_id, None, "no-such-job"])

    assert all(e != "job_updated" for _, e, _ in captured)
