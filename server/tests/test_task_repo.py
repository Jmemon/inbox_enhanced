"""task_engine.repo tests: task/link/entity/event CRUD scoping, upsert_link's
sticky origin rule, and refold_entity's fold-order semantics. Also covers the
formulate_criteria relocation to task_engine.criteria (bucket_repo keeps a
re-export shim — see test_bucket_repo.py, which must stay green unmodified)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import InboxThread, TaskThreadLink, User
from app.inbox import bucket_repo
from app.task_engine import criteria, repo


def _mk_user(db, uid="u1"):
    user = User(id=uid, email=f"{uid}@x.com", created_at=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    return user


def _mk_thread(db, uid="u1", tid="th1", gmail_id="g1"):
    thread = InboxThread(id=tid, user_id=uid, gmail_id=gmail_id, subject="hi")
    db.add(thread)
    db.commit()
    return thread


@pytest.fixture
def two_users(db):
    _mk_user(db, "u1")
    _mk_user(db, "u2")
    return db


def _mk_task(db, uid="u1", name="T", state_schema=None, kind="tracker"):
    return repo.create_task(
        db, user_id=uid, name=name, goal="", criteria="", state_schema=state_schema, kind=kind
    )


# ---------------------------------------------------------------------------
# create/list/get scoping
# ---------------------------------------------------------------------------


def test_create_task_sets_defaults(two_users):
    task = _mk_task(two_users, name="Visa")
    two_users.commit()
    assert task.id
    assert task.kind == "tracker"
    assert task.status == "active"
    assert task.version == 1
    assert task.is_deleted is False
    assert task.created_at is not None


def test_get_owned_task_invisible_to_other_user(two_users):
    task = _mk_task(two_users, uid="u1")
    two_users.commit()
    assert repo.get_owned_task(two_users, user_id="u2", task_id=task.id) is None
    got = repo.get_owned_task(two_users, user_id="u1", task_id=task.id)
    assert got is not None and got.id == task.id


def test_get_owned_task_excludes_soft_deleted(two_users):
    task = _mk_task(two_users, uid="u1")
    task.is_deleted = True
    two_users.commit()
    assert repo.get_owned_task(two_users, user_id="u1", task_id=task.id) is None


def test_list_tasks_scoped_sorted_by_name_excludes_deleted_and_other_users(two_users):
    _mk_task(two_users, uid="u1", name="Zeta")
    _mk_task(two_users, uid="u1", name="Alpha")
    gone = _mk_task(two_users, uid="u1", name="Gone")
    gone.is_deleted = True
    _mk_task(two_users, uid="u2", name="Theirs")
    two_users.commit()

    names = [t.name for t in repo.list_tasks(two_users, user_id="u1")]
    assert names == ["Alpha", "Zeta"]


def test_list_tasks_includes_paused(two_users):
    paused = _mk_task(two_users, uid="u1", name="Paused")
    paused.status = "paused"
    two_users.commit()
    names = [t.name for t in repo.list_tasks(two_users, user_id="u1")]
    assert names == ["Paused"]


def test_list_tasks_filters_by_kind(two_users):
    _mk_task(two_users, uid="u1", name="Tracker1", kind="tracker")
    _mk_task(two_users, uid="u1", name="Bucket1", kind="bucket")
    two_users.commit()
    names = [t.name for t in repo.list_tasks(two_users, user_id="u1", kind="bucket")]
    assert names == ["Bucket1"]


def test_list_active_trackers_requires_active_tracker_with_schema(two_users):
    schema = {"version": 1, "pipeline": {"stages": ["todo"], "terminal": ["done"]}}
    _mk_task(two_users, uid="u1", name="WithSchema", state_schema=schema)
    _mk_task(two_users, uid="u1", name="NoSchema", state_schema=None)
    paused = _mk_task(two_users, uid="u1", name="Paused", state_schema=schema)
    paused.status = "paused"
    bucket = _mk_task(two_users, uid="u1", name="BucketKind", state_schema=schema, kind="bucket")
    two_users.commit()

    names = [t.name for t in repo.list_active_trackers(two_users, user_id="u1")]
    assert names == ["WithSchema"]


def test_bump_version_increments_and_returns_new_value(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    assert repo.bump_version(two_users, task=task) == 2
    assert task.version == 2
    assert repo.bump_version(two_users, task=task) == 3


# ---------------------------------------------------------------------------
# upsert_link — the sticky rule
# ---------------------------------------------------------------------------


def test_upsert_link_inserts_new_row(two_users):
    task = _mk_task(two_users)
    thread = _mk_thread(two_users)
    two_users.commit()

    link = repo.upsert_link(
        two_users, task_id=task.id, thread_id=thread.id, user_id="u1", origin="llm", confidence=80
    )
    two_users.commit()

    assert link is not None
    assert link.origin == "llm"
    assert link.state == "attached"
    assert link.confidence == 80


def test_upsert_link_llm_over_existing_user_row_is_sticky_noop(two_users):
    task = _mk_task(two_users)
    thread = _mk_thread(two_users)
    two_users.commit()

    repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1", origin="user")
    two_users.commit()

    result = repo.upsert_link(
        two_users, task_id=task.id, thread_id=thread.id, user_id="u1",
        origin="llm", state="detached", confidence=10,
    )
    two_users.commit()

    assert result is None
    row = repo.get_link(two_users, task_id=task.id, thread_id=thread.id)
    assert row.origin == "user"
    assert row.state == "attached"
    assert row.confidence is None


def test_upsert_link_user_over_existing_llm_row_updates(two_users):
    task = _mk_task(two_users)
    thread = _mk_thread(two_users)
    two_users.commit()

    repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1",
                      origin="llm", confidence=50)
    two_users.commit()

    result = repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1",
                               origin="user", state="detached")
    two_users.commit()

    assert result is not None
    assert result.origin == "user"
    assert result.state == "detached"


def test_upsert_link_llm_over_existing_llm_row_updates(two_users):
    task = _mk_task(two_users)
    thread = _mk_thread(two_users)
    two_users.commit()

    repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1",
                      origin="llm", confidence=50)
    two_users.commit()

    result = repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1",
                               origin="llm", confidence=90)
    two_users.commit()

    assert result is not None
    assert result.confidence == 90


def test_upsert_link_idempotent_on_uq_task_thread(two_users):
    """Repeated upserts on the same (task_id, thread_id) never create a
    second row — the unique constraint's row stays singular."""
    task = _mk_task(two_users)
    thread = _mk_thread(two_users)
    two_users.commit()

    repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1", origin="llm")
    two_users.commit()
    repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1",
                      origin="llm", state="detached", confidence=90)
    two_users.commit()

    rows = two_users.execute(
        select(TaskThreadLink).where(TaskThreadLink.task_id == task.id)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].state == "detached"
    assert rows[0].confidence == 90


def test_list_attached_thread_ids_excludes_detached(two_users):
    task = _mk_task(two_users)
    thread = _mk_thread(two_users)
    two_users.commit()

    repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1", origin="llm")
    two_users.commit()
    assert repo.list_attached_thread_ids(two_users, task_id=task.id) == {thread.id}

    repo.upsert_link(two_users, task_id=task.id, thread_id=thread.id, user_id="u1",
                      origin="llm", state="detached")
    two_users.commit()
    assert repo.list_attached_thread_ids(two_users, task_id=task.id) == set()


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------


def test_get_or_create_entity_is_idempotent(two_users):
    task = _mk_task(two_users)
    two_users.commit()

    e1 = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                    entity_key="_self", display_name="Self")
    two_users.commit()
    e2 = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                    entity_key="_self", display_name="Self")

    assert e1.id == e2.id
    assert e1.state == {}


def test_list_entities_sorted_by_updated_at_desc(two_users):
    task = _mk_task(two_users)
    two_users.commit()

    e_a = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                     entity_key="a", display_name="A")
    two_users.commit()
    e_b = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                     entity_key="b", display_name="B")
    two_users.commit()

    e_a.updated_at = datetime.now(timezone.utc) + timedelta(seconds=10)
    two_users.commit()

    entities = repo.list_entities(two_users, task_id=task.id)
    assert [e.entity_key for e in entities] == ["a", "b"]


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_append_event_flushes_id_and_does_not_apply(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()

    event = repo.append_event(
        two_users, task=task, entity=entity, origin="llm", status="pending_review",
        field="stage", old_value=None, new_value="applied", evidence_quote="quoted text",
        confidence=70,
    )

    assert event.id  # flushed before commit
    assert event.status == "pending_review"
    assert event.task_id == task.id
    assert event.entity_id == entity.id
    assert entity.state == {}  # append does not apply


def test_apply_event_updates_entity_state_and_bumps_task_version(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()
    event = repo.append_event(two_users, task=task, entity=entity, origin="llm",
                               status="pending_review", field="stage", new_value="applied")
    two_users.commit()
    version_before = task.version

    repo.apply_event(two_users, task=task, entity=entity, event=event)
    two_users.commit()

    assert event.status == "applied"
    assert entity.state["stage"] == "applied"
    assert task.version == version_before + 1


def test_list_events_newest_first_with_status_and_entity_filters(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    e1 = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                    entity_key="a", display_name="A")
    e2 = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                    entity_key="b", display_name="B")
    two_users.commit()

    ev1 = repo.append_event(two_users, task=task, entity=e1, origin="llm",
                             status="pending_review", field="stage", new_value="x")
    ev1.created_at = datetime.now(timezone.utc)
    ev2 = repo.append_event(two_users, task=task, entity=e2, origin="llm",
                             status="applied", field="stage", new_value="y")
    ev2.created_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    two_users.commit()

    all_events = repo.list_events(two_users, task_id=task.id)
    assert [e.id for e in all_events] == [ev2.id, ev1.id]  # newest first

    pending = repo.list_events(two_users, task_id=task.id, status="pending_review")
    assert [e.id for e in pending] == [ev1.id]

    for_e2 = repo.list_events(two_users, task_id=task.id, entity_id=e2.id)
    assert [e.id for e in for_e2] == [ev2.id]


def test_pending_count_counts_only_pending_review(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()
    repo.append_event(two_users, task=task, entity=entity, origin="llm",
                       status="pending_review", field="stage", new_value="x")
    repo.append_event(two_users, task=task, entity=entity, origin="llm",
                       status="pending_review", field="notes", new_value="y")
    repo.append_event(two_users, task=task, entity=entity, origin="llm",
                       status="applied", field="stage", new_value="z")
    two_users.commit()

    assert repo.pending_count(two_users, task_id=task.id) == 2


# ---------------------------------------------------------------------------
# refold_entity
# ---------------------------------------------------------------------------


def test_refold_entity_drops_field_whose_only_applied_event_is_reverted(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()

    t0 = datetime.now(timezone.utc)
    e1 = repo.append_event(two_users, task=task, entity=entity, origin="llm", status="applied",
                            field="stage", new_value="applied")
    e1.created_at = t0
    # the middle event, and the only write ever made to 'notes'
    e2 = repo.append_event(two_users, task=task, entity=entity, origin="llm", status="applied",
                            field="notes", new_value="called recruiter")
    e2.created_at = t0 + timedelta(seconds=5)
    e3 = repo.append_event(two_users, task=task, entity=entity, origin="llm", status="applied",
                            field="stage", new_value="interview")
    e3.created_at = t0 + timedelta(seconds=10)
    two_users.commit()

    # revert the middle event (simulating a user "undo" of that one change)
    e2.status = "reverted"
    two_users.commit()
    version_before = task.version

    repo.refold_entity(two_users, task=task, entity=entity)
    two_users.commit()

    # 'notes' has no surviving applied event -> removed entirely; 'stage'
    # reflects the fold over the two survivors (e1, e3).
    assert entity.state == {"stage": "interview"}
    assert task.version == version_before + 1


def test_refold_entity_user_origin_wins_created_at_tie_regardless_of_insertion_order(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()

    tie_ts = datetime.now(timezone.utc)
    # Insert the user-origin event FIRST — a naive created_at-only sort (or a
    # sort that trusted insertion/row order) would let it be overwritten by
    # the llm event below. The (created_at, origin) tie-break must still
    # place 'user' after 'llm' at equal timestamps.
    user_event = repo.append_event(two_users, task=task, entity=entity, origin="user",
                                    status="applied", field="stage", new_value="offer")
    user_event.created_at = tie_ts
    llm_event = repo.append_event(two_users, task=task, entity=entity, origin="llm",
                                   status="applied", field="stage", new_value="interview")
    llm_event.created_at = tie_ts
    two_users.commit()

    repo.refold_entity(two_users, task=task, entity=entity)
    two_users.commit()

    assert entity.state["stage"] == "offer"


def test_refold_entity_stage_falls_back_to_none_with_no_surviving_event(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()
    event = repo.append_event(two_users, task=task, entity=entity, origin="llm", status="applied",
                               field="stage", new_value="applied")
    two_users.commit()
    event.status = "reverted"
    two_users.commit()

    repo.refold_entity(two_users, task=task, entity=entity)
    two_users.commit()

    assert entity.state == {"stage": None}


# ---------------------------------------------------------------------------
# recent_user_events (Task 2, spec §4.6 learning loop)
# ---------------------------------------------------------------------------


def test_recent_user_events_returns_only_user_applied_newest_first(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()

    t0 = datetime.now(timezone.utc)

    # Noise this query must exclude: llm-origin applied, and user-origin but
    # not applied (pending_review / rejected).
    llm_applied = repo.append_event(two_users, task=task, entity=entity, origin="llm",
                                     status="applied", field="stage", new_value="applied")
    llm_applied.created_at = t0
    user_pending = repo.append_event(two_users, task=task, entity=entity, origin="user",
                                      status="pending_review", field="stage", new_value="interview")
    user_pending.created_at = t0 + timedelta(seconds=1)

    user_ev1 = repo.append_event(two_users, task=task, entity=entity, origin="user",
                                  status="applied", field="stage", new_value="interview")
    user_ev1.created_at = t0 + timedelta(seconds=2)
    user_ev2 = repo.append_event(two_users, task=task, entity=entity, origin="user",
                                  status="applied", field="stage", new_value="offer")
    user_ev2.created_at = t0 + timedelta(seconds=3)
    two_users.commit()

    events = repo.recent_user_events(two_users, task_id=task.id)
    assert [e.id for e in events] == [user_ev2.id, user_ev1.id]  # newest first


def test_recent_user_events_respects_limit(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    entity = repo.get_or_create_entity(two_users, task_id=task.id, user_id="u1",
                                        entity_key="_self", display_name="Self")
    two_users.commit()

    t0 = datetime.now(timezone.utc)
    ids = []
    for i in range(7):
        ev = repo.append_event(two_users, task=task, entity=entity, origin="user",
                                status="applied", field="stage", new_value=str(i))
        ev.created_at = t0 + timedelta(seconds=i)
        ids.append(ev.id)
    two_users.commit()

    events = repo.recent_user_events(two_users, task_id=task.id, limit=3)
    assert [e.id for e in events] == list(reversed(ids))[:3]


def test_recent_user_events_empty_when_no_corrections(two_users):
    task = _mk_task(two_users)
    two_users.commit()
    assert repo.recent_user_events(two_users, task_id=task.id) == []


# ---------------------------------------------------------------------------
# criteria relocation
# ---------------------------------------------------------------------------


def test_bucket_repo_formulate_criteria_is_the_relocated_function():
    assert bucket_repo.formulate_criteria is criteria.formulate_criteria
