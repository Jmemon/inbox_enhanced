"""Task 7: the decoupled extraction pipeline (`app.task_engine.engine.
extract_for_pair` + the Celery tasks in `app.workers.task_engine_tasks`).

Uses eager celery + a file-backed sqlite session_factory (shared across the
two `.apply()` calls a "re-run" test makes) + fakeredis + a monkeypatched
`llm_client.call_messages` returning canned extraction JSON. `_publish` is
captured via `app.workers.tasks._publish` (task_engine_tasks late-imports it
at call time to avoid the tasks<->task_engine_tasks import cycle — see that
module's docstring — so patching the attribute on `app.workers.tasks` is what
the late import actually picks up).
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
from app.task_engine.schema import PipelineSpec, TaskStateSchema
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


def _seed_tracker(session_factory, *, user_id=USER_ID, status="active"):
    schema = _singleton_schema()
    db = session_factory()
    task = task_repo.create_task(
        db, user_id=user_id, name="Job tracker", goal="Land the job",
        criteria="", state_schema=schema.model_dump(), kind="tracker",
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
