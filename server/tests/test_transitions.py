"""Exhaustive tests for the pure 8-step mechanical validator
(app.task_engine.transitions.validate_and_stage), spec §4.4. Proposals are
built as plain dicts (no LLM, no extract_transition.parse_response) — the
validator's contract is "trust nothing", so these tests poke it directly.

Uses the real `db` fixture (SQLite create_all) because entities/events are
real rows via task_engine.repo, but ParsedThread/ParsedMessage are built as
plain dataclasses and message_id_map is a synthetic dict — TaskEvent.message_id
and thread_id are unconstrained soft pointers (no FK), so no InboxThread/
InboxMessage rows are needed at all.
"""

from datetime import datetime, timezone

import pytest

from app.db.models import Task, User
from app.gmail.parser import ParsedMessage, ParsedThread
from app.task_engine import repo
from app.task_engine.schema import AttributeSpec, EntitySpec, PipelineSpec, TaskStateSchema
from app.task_engine.transitions import StagedResult, normalize_key, similarity, validate_and_stage

THREAD_ROW_ID = "th1"


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------


def _mk_user(db, uid="u1") -> User:
    user = User(id=uid, email=f"{uid}@x.com", created_at=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    return user


def _mk_task(db, *, schema: TaskStateSchema, uid="u1", name="Job hunt", goal="Land a job") -> Task:
    task = repo.create_task(
        db, user_id=uid, name=name, goal=goal, criteria="", state_schema=schema.model_dump(), kind="tracker",
    )
    db.commit()
    return task


def multi_entity_schema() -> TaskStateSchema:
    return TaskStateSchema(
        version=1,
        entity=EntitySpec(
            noun="company",
            identity_hint="the hiring company",
            attributes=[
                AttributeSpec(key="level", type="enum", values=["junior", "mid", "senior"]),
                AttributeSpec(key="next_step_date", type="datetime"),
            ],
        ),
        pipeline=PipelineSpec(stages=["applied", "interview", "onsite"], terminal=["offer", "rejected"]),
    )


def singleton_schema() -> TaskStateSchema:
    return TaskStateSchema(
        version=1, entity=None,
        pipeline=PipelineSpec(stages=["todo", "in_progress"], terminal=["done"]),
    )


def _msg(gid, ts, body, subject="Re: thread") -> ParsedMessage:
    return ParsedMessage(
        gmail_message_id=gid, gmail_thread_id="gt1", gmail_internal_date=ts,
        gmail_history_id="h1", subject=subject, from_addr="hr@corp.example", to_addr="me@x.com",
        body_text=body, body_preview=body[:150],
    )


def _thread(*messages: ParsedMessage) -> ParsedThread:
    return ParsedThread(
        gmail_thread_id="gt1", subject=messages[0].subject if messages else None,
        recent_internal_date=messages[-1].gmail_internal_date if messages else 0,
        messages=list(messages),
    )


def _proposal(
    entity="stripe", is_new=False, field="stage", new_value="interview",
    evidence="moving to the interview stage", message_id="gm1", confidence=90,
) -> dict:
    return {
        "entity": entity, "is_new_entity": is_new, "field": field, "new_value": new_value,
        "evidence_quote": evidence, "message_id": message_id, "confidence": confidence,
    }


# The default two-message thread reused by most tests below.
M1 = _msg("gm1", 1_000_000, "We received your application to Stripe, Inc. Thanks for applying.")
M2 = _msg("gm2", 2_000_000, "Good news, you are moving to the interview stage.")
DEFAULT_THREAD = _thread(M1, M2)
DEFAULT_MAP = {"gm1": "im1", "gm2": "im2"}


# ---------------------------------------------------------------------------
# normalize_key / similarity primitives
# ---------------------------------------------------------------------------


def test_normalize_key_casefolds_strips_punctuation_and_collapses_whitespace():
    assert normalize_key("Stripe, Inc.") == "stripe inc"
    assert normalize_key("  ACME   Corp!!  ") == "acme corp"


def test_similarity_is_symmetric_ratio():
    assert similarity("stripe", "stripe") == 1.0
    assert similarity("stripe inc", "stripe") == similarity("stripe", "stripe inc")


# ---------------------------------------------------------------------------
# Step 1: shape
# ---------------------------------------------------------------------------


def test_unknown_field_dropped(db):
    _mk_user(db)
    task = _mk_task(db, schema=multi_entity_schema())
    result = validate_and_stage(
        db, task=task, schema=multi_entity_schema(), parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(field="not_a_real_field", new_value="whatever")], message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []


def test_bad_enum_value_dropped(db):
    _mk_user(db)
    task = _mk_task(db, schema=multi_entity_schema())
    result = validate_and_stage(
        db, task=task, schema=multi_entity_schema(), parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(field="level", new_value="ceo")], message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []


def test_bad_datetime_value_dropped(db):
    _mk_user(db)
    task = _mk_task(db, schema=multi_entity_schema())
    result = validate_and_stage(
        db, task=task, schema=multi_entity_schema(), parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(field="next_step_date", new_value="not-a-date")], message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []


def test_stage_not_in_schema_dropped(db):
    _mk_user(db)
    task = _mk_task(db, schema=multi_entity_schema())
    result = validate_and_stage(
        db, task=task, schema=multi_entity_schema(), parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(field="stage", new_value="bogus_stage")], message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []


def test_malformed_proposal_does_not_create_an_entity(db):
    """A shape-drop (step 1) must exit before entity resolution (step 2) —
    no 'Ghost Corp' entity should be minted for a proposal that never gets
    past the field check."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="Ghost Corp", is_new=True, field="not_real", new_value="x")],
        message_id_map=DEFAULT_MAP,
    )
    assert repo.list_entities(db, task_id=task.id) == []


# ---------------------------------------------------------------------------
# Step 1b: message reference (foreign / hallucinated gmail message_id)
# ---------------------------------------------------------------------------


def test_foreign_message_id_dropped(db):
    """A proposal citing a gmail_message_id that isn't among this thread's
    parsed messages must be dropped outright. Letting it through would
    resolve to internal_message_id=None at step 7 (wrong attribution to the
    audit log, and never deduped against a future rerun of the same
    hallucinated citation)."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", message_id="gm-does-not-exist")],
        message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []


# ---------------------------------------------------------------------------
# Step 2: entity resolution
# ---------------------------------------------------------------------------


def test_singleton_schema_routes_to_self(db):
    _mk_user(db)
    schema = singleton_schema()
    task = _mk_task(db, schema=schema, name="Visa tracker")
    thread = _thread(_msg("gm1", 1_000, "Your application has moved to in_progress review."))
    result = validate_and_stage(
        db, task=task, schema=schema, parsed=thread, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(
            entity="ignored name", field="stage", new_value="in_progress",
            evidence="moved to in_progress review", message_id="gm1", confidence=90,
        )],
        message_id_map={"gm1": "im1"},
    )
    entities = repo.list_entities(db, task_id=task.id)
    assert len(entities) == 1
    assert entities[0].entity_key == "_self"
    assert len(result.applied) == 1
    assert result.applied[0].entity_id == entities[0].id
    assert entities[0].state["stage"] == "in_progress"


def test_exact_entity_key_match_reuses_existing_row(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    existing = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", is_new=False)], message_id_map=DEFAULT_MAP,
    )
    assert len(repo.list_entities(db, task_id=task.id)) == 1
    assert result.applied[0].entity_id == existing.id


def test_fuzzy_match_above_point_six_reuses_existing_entity(db):
    """'Stripe, Inc.' normalizes to 'stripe inc'; similarity('stripe inc',
    'stripe') == 0.75, well above the 0.6 auto-match threshold."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    existing = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        # is_new_entity=True is deliberately wrong here — the fuzzy match must
        # win over the LLM's (mistaken) is_new_entity flag.
        proposals=[_proposal(entity="Stripe, Inc.", is_new=True)], message_id_map=DEFAULT_MAP,
    )
    assert len(repo.list_entities(db, task_id=task.id)) == 1
    assert result.applied[0].entity_id == existing.id


def test_similarity_between_thresholds_routes_to_pending_on_closest(db):
    """'Stripewise Corp' -> 'stripewise corp'; similarity(..., 'stripe') ==
    0.571, in [0.4, 0.6) -> pending_review on the closest entity, no
    duplicate minted, even though is_new_entity=True."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    existing = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="Stripewise Corp", is_new=True)], message_id_map=DEFAULT_MAP,
    )
    assert len(repo.list_entities(db, task_id=task.id)) == 1  # no duplicate created
    assert result.applied == []
    assert len(result.pending) == 1
    assert result.pending[0].entity_id == existing.id


def test_low_similarity_with_is_new_entity_creates_new_entity(db):
    """'Acme Corp' vs 'stripe' similarity is 0.133, well under 0.4 -> since
    is_new_entity=True, a new entity is minted with the verbatim display name."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    thread = _thread(_msg("gm1", 1_000, "We received your application to Acme Corp."))
    result = validate_and_stage(
        db, task=task, schema=schema, parsed=thread, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(
            entity="Acme Corp", is_new=True, field="stage", new_value="applied",
            evidence="received your application to Acme Corp", message_id="gm1",
        )],
        message_id_map={"gm1": "im1"},
    )
    entities = {e.entity_key: e for e in repo.list_entities(db, task_id=task.id)}
    assert set(entities) == {"stripe", "acme corp"}
    assert entities["acme corp"].display_name == "Acme Corp"
    assert len(result.applied) == 1


def test_new_entity_with_fabricated_evidence_does_not_mint_entity(db):
    """Regression: is_new_entity=True + low similarity + evidence that does
    not appear in the thread must be dropped at step 4 WITHOUT ever
    persisting an entity row. Before the fix, step 2's create branch called
    repo.get_or_create_entity() immediately — a fabricated-evidence proposal
    for a brand new entity would leave a phantom, evidence-free row that
    silently appears on the next board load even though its only event was
    dropped."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(
            # similarity('zylo dynamics', 'stripe') ~= 0.105, well under the
            # 0.4 pending threshold, so this genuinely hits the create-new
            # branch (not the forced-pending-on-closest-match branch).
            entity="Zylo Dynamics", is_new=True, field="stage", new_value="applied",
            evidence="this sentence appears nowhere in the thread", message_id="gm1",
        )],
        message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []
    entity_keys = {e.entity_key for e in repo.list_entities(db, task_id=task.id)}
    assert entity_keys == {"stripe"}  # no phantom "zylo dynamics" row


def test_new_entity_survives_evidence_creates_and_touches_entity_ids(db):
    """Companion to the above: when a brand-new entity's proposal DOES
    survive every guard, the entity is created (at step 8, immediately
    before the event is written) and its id lands in touched_entity_ids."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)

    thread = _thread(_msg("gm1", 1_000, "We received your application to Acme Corp."))
    result = validate_and_stage(
        db, task=task, schema=schema, parsed=thread, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(
            entity="Acme Corp", is_new=True, field="stage", new_value="applied",
            evidence="received your application to Acme Corp", message_id="gm1",
        )],
        message_id_map={"gm1": "im1"},
    )
    entities = repo.list_entities(db, task_id=task.id)
    assert len(entities) == 1 and entities[0].entity_key == "acme corp"
    assert len(result.applied) == 1
    assert result.touched_entity_ids == {entities[0].id}


def test_low_similarity_without_is_new_entity_dropped(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="Acme Corp", is_new=False)], message_id_map=DEFAULT_MAP,
    )
    assert len(repo.list_entities(db, task_id=task.id)) == 1  # nothing new created
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []


# ---------------------------------------------------------------------------
# Step 3: stage legality
# ---------------------------------------------------------------------------


def test_backward_stage_move_forced_pending_despite_high_confidence(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    entity.state = {"stage": "onsite"}
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="applied", confidence=95,
                              evidence="received your application to Stripe, Inc.", message_id="gm1")],
        message_id_map=DEFAULT_MAP,
    )
    assert result.applied == []
    assert len(result.pending) == 1
    assert entity.state == {"stage": "onsite"}  # untouched


def test_terminal_entity_locked_forced_pending_despite_high_confidence(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    entity.state = {"stage": "offer"}
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="rejected", confidence=95)],
        message_id_map=DEFAULT_MAP,
    )
    assert result.applied == []
    assert len(result.pending) == 1
    assert entity.state == {"stage": "offer"}  # untouched


# ---------------------------------------------------------------------------
# Step 4: evidence
# ---------------------------------------------------------------------------


def test_fabricated_evidence_dropped(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", evidence="this sentence appears nowhere in the thread")],
        message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []


def test_evidence_check_is_whitespace_normalized(db):
    """A quote that matches the thread text except for wrapping/extra
    whitespace still passes — whitespace normalization is a substring check,
    not exact-string."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", evidence="moving   to  the\ninterview   stage")],
        message_id_map=DEFAULT_MAP,
    )
    assert len(result.applied) == 1


# ---------------------------------------------------------------------------
# Step 5: correction fences
# ---------------------------------------------------------------------------


def test_fence_blocks_older_message_but_newer_message_passes(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    # A user correction applied at ms epoch 1,500,000 — strictly between
    # M1 (ts=1,000,000) and M2 (ts=2,000,000).
    fence_event = repo.append_event(
        db, task=task, entity=entity, origin="user", status="applied",
        field="stage", new_value="applied",
    )
    fence_event.created_at = datetime.fromtimestamp(1500, tz=timezone.utc)
    db.commit()

    older_result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(
            entity="stripe", field="stage", new_value="interview", confidence=95,
            evidence="received your application to Stripe, Inc.", message_id="gm1",
        )],
        message_id_map=DEFAULT_MAP,
    )
    assert older_result.applied == []
    assert len(older_result.pending) == 1

    newer_result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(
            entity="stripe", field="stage", new_value="interview", confidence=95,
            evidence="moving to the interview stage", message_id="gm2",
        )],
        message_id_map=DEFAULT_MAP,
    )
    assert len(newer_result.applied) == 1
    assert entity.state["stage"] == "interview"


def test_fence_at_exact_message_timestamp_blocks_strictly_newer_contract(db):
    """Pin the 'strictly newer' contract at its boundary: a proposal whose
    evidence message shares the EXACT same timestamp as the fencing user
    correction must still be forced to pending_review — the comparison is
    `>`, not `>=`."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    fence_event = repo.append_event(
        db, task=task, entity=entity, origin="user", status="applied",
        field="stage", new_value="applied",
    )
    # M2's gmail_internal_date is 2_000_000 ms == 2000s epoch — exactly equal,
    # not older, not newer.
    fence_event.created_at = datetime.fromtimestamp(2000, tz=timezone.utc)
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(
            entity="stripe", field="stage", new_value="interview", confidence=95,
            evidence="moving to the interview stage", message_id="gm2",
        )],
        message_id_map=DEFAULT_MAP,
    )
    assert result.applied == []
    assert len(result.pending) == 1
    assert entity.state == {}  # untouched


def test_no_fence_when_no_prior_user_event(db):
    """A never-corrected entity has nothing to fence against — normal
    confidence-gated processing applies."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", message_id="gm1",
                              evidence="received your application to Stripe, Inc.", confidence=95)],
        message_id_map=DEFAULT_MAP,
    )
    assert len(result.applied) == 1


# ---------------------------------------------------------------------------
# Step 6: no-op
# ---------------------------------------------------------------------------


def test_noop_skipped_silently_no_event_written(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    entity.state = {"stage": "interview"}
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="interview", confidence=95)],
        message_id_map=DEFAULT_MAP,
    )
    assert result.dropped == 1
    assert result.applied == [] and result.pending == []
    assert repo.list_events(db, task_id=task.id) == []  # no event row at all


# ---------------------------------------------------------------------------
# Step 7: idempotency
# ---------------------------------------------------------------------------


def test_duplicate_task_message_field_skipped_on_rerun(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    proposal = _proposal(
        entity="stripe", field="stage", new_value="interview", confidence=50,  # below threshold -> pending
        evidence="moving to the interview stage", message_id="gm2",
    )
    first = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[proposal], message_id_map=DEFAULT_MAP,
    )
    db.commit()
    assert len(first.pending) == 1 and first.dropped == 0

    second = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[proposal], message_id_map=DEFAULT_MAP,
    )
    assert second.dropped == 1
    assert second.applied == [] and second.pending == []
    assert len(repo.list_events(db, task_id=task.id)) == 1  # still just the one event


# ---------------------------------------------------------------------------
# Step 8: confidence gate
# ---------------------------------------------------------------------------


def test_high_confidence_applies_updates_state_and_bumps_version(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()
    version_before = task.version

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="interview", confidence=90,
                              evidence="moving to the interview stage", message_id="gm2")],
        message_id_map=DEFAULT_MAP,
    )
    assert len(result.applied) == 1 and result.pending == []
    assert result.applied[0].status == "applied"
    assert entity.state["stage"] == "interview"
    assert task.version == version_before + 1
    assert result.touched_entity_ids == {entity.id}


def test_low_confidence_pending_state_untouched(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()
    version_before = task.version

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="interview", confidence=50,
                              evidence="moving to the interview stage", message_id="gm2")],
        message_id_map=DEFAULT_MAP,
    )
    assert result.applied == []
    assert len(result.pending) == 1
    assert result.pending[0].status == "pending_review"
    assert entity.state == {}  # untouched
    assert task.version == version_before


# ---------------------------------------------------------------------------
# Every event carries full provenance; mixed-batch invariant
# ---------------------------------------------------------------------------


def test_event_provenance_fields(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="interview", confidence=90,
                              evidence="moving to the interview stage", message_id="gm2")],
        message_id_map=DEFAULT_MAP,
    )
    event = result.applied[0]
    assert event.thread_id == THREAD_ROW_ID
    assert event.message_id == "im2"          # internal id, resolved via message_id_map
    assert event.gmail_message_id == "gm2"     # denormalized gmail id
    assert event.evidence_quote == "moving to the interview stage"
    assert event.confidence == 90
    assert event.origin == "llm"


def test_mixed_batch_invariant_applied_plus_pending_plus_dropped_equals_total(db):
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    proposals = [
        _proposal(field="unknown_field", new_value="x"),  # step 1 drop
        _proposal(entity="stripe", field="stage", new_value="interview", confidence=90,
                   evidence="moving to the interview stage", message_id="gm2"),  # applied
        _proposal(entity="stripe", field="stage", new_value="onsite", confidence=40,
                   evidence="moving to the interview stage", message_id="gm2"),  # dup field -> idempotent drop
                                                                                  # after the first proposal applies
    ]
    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=proposals, message_id_map=DEFAULT_MAP,
    )
    total_accounted = len(result.applied) + len(result.pending) + result.dropped
    assert total_accounted == len(proposals)


# ---------------------------------------------------------------------------
# Pending provenance: pending_reason + proposed_entity (task 1, phase 2B)
#
# Every pending_review event must carry the reason for the FIRST guard that
# forced it to pending — steps 2 (near_duplicate_entity), 3 (backward_move /
# terminal_locked), 5 (fence_blocked), or, when nothing earlier forced it,
# step 8's confidence gate (low_confidence). Each scenario below is one of
# these five reasons in isolation, built from the same fixtures used above.
# ---------------------------------------------------------------------------


def _near_duplicate_case(db):
    """Step 2: 'Stripewise Corp' lands in [0.4, 0.6) similarity to 'stripe' —
    routes to pending on the closest existing entity."""
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()
    proposals = [_proposal(entity="Stripewise Corp", is_new=True)]
    return task, schema, DEFAULT_THREAD, proposals, DEFAULT_MAP


def _backward_move_case(db):
    """Step 3: a stage move earlier in the pipeline than the entity's
    current stage, despite high confidence."""
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    entity.state = {"stage": "onsite"}
    db.commit()
    proposals = [_proposal(entity="stripe", field="stage", new_value="applied", confidence=95,
                            evidence="received your application to Stripe, Inc.", message_id="gm1")]
    return task, schema, DEFAULT_THREAD, proposals, DEFAULT_MAP


def _terminal_locked_case(db):
    """Step 3: the entity is already in a terminal stage — only a user may
    move it further."""
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    entity.state = {"stage": "offer"}
    db.commit()
    proposals = [_proposal(entity="stripe", field="stage", new_value="rejected", confidence=95)]
    return task, schema, DEFAULT_THREAD, proposals, DEFAULT_MAP


def _fence_blocked_case(db):
    """Step 5: a prior user correction fences this entity; the proposal's
    evidence message is not strictly newer than it."""
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()
    fence_event = repo.append_event(
        db, task=task, entity=entity, origin="user", status="applied",
        field="stage", new_value="applied",
    )
    fence_event.created_at = datetime.fromtimestamp(1500, tz=timezone.utc)  # between M1 and M2
    db.commit()
    proposals = [_proposal(
        entity="stripe", field="stage", new_value="interview", confidence=95,
        evidence="received your application to Stripe, Inc.", message_id="gm1",
    )]
    return task, schema, DEFAULT_THREAD, proposals, DEFAULT_MAP


def _low_confidence_case(db):
    """Step 8: nothing earlier forced pending; confidence alone is below
    the apply threshold."""
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()
    proposals = [_proposal(entity="stripe", field="stage", new_value="interview", confidence=50,
                            evidence="moving to the interview stage", message_id="gm2")]
    return task, schema, DEFAULT_THREAD, proposals, DEFAULT_MAP


@pytest.mark.parametrize("build_case,expected_reason", [
    (_near_duplicate_case, "near_duplicate_entity"),
    (_backward_move_case, "backward_move"),
    (_terminal_locked_case, "terminal_locked"),
    (_fence_blocked_case, "fence_blocked"),
    (_low_confidence_case, "low_confidence"),
])
def test_pending_event_carries_reason_for_first_forcing_guard(db, build_case, expected_reason):
    _mk_user(db)
    task, schema, thread, proposals, message_id_map = build_case(db)

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=thread, thread_row_id=THREAD_ROW_ID,
        proposals=proposals, message_id_map=message_id_map,
    )

    assert result.applied == []
    assert len(result.pending) == 1
    assert result.pending[0].pending_reason == expected_reason


def test_near_duplicate_pending_carries_llms_verbatim_proposed_entity(db):
    _mk_user(db)
    task, schema, thread, proposals, message_id_map = _near_duplicate_case(db)

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=thread, thread_row_id=THREAD_ROW_ID,
        proposals=proposals, message_id_map=message_id_map,
    )

    assert result.pending[0].proposed_entity == "Stripewise Corp"


@pytest.mark.parametrize("build_case", [
    _backward_move_case, _terminal_locked_case, _fence_blocked_case, _low_confidence_case,
])
def test_non_near_duplicate_pendings_have_no_proposed_entity(db, build_case):
    _mk_user(db)
    task, schema, thread, proposals, message_id_map = build_case(db)

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=thread, thread_row_id=THREAD_ROW_ID,
        proposals=proposals, message_id_map=message_id_map,
    )

    assert result.pending[0].proposed_entity is None


def test_applied_events_carry_no_pending_reason_or_proposed_entity(db):
    """A cleanly-applied event (no guard forced it, confidence clears the
    threshold) leaves both new columns None."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="interview", confidence=90,
                              evidence="moving to the interview stage", message_id="gm2")],
        message_id_map=DEFAULT_MAP,
    )
    assert len(result.applied) == 1
    assert result.applied[0].pending_reason is None
    assert result.applied[0].proposed_entity is None


def test_backward_move_beats_low_confidence_first_guard_wins(db):
    """A backward stage move (step 3) at LOW confidence must still report
    'backward_move', not 'low_confidence' — step 3 runs before step 8, so
    it claims the reason first regardless of what the confidence gate would
    have said on its own."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    entity.state = {"stage": "onsite"}
    db.commit()

    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="stripe", field="stage", new_value="applied", confidence=10,
                              evidence="received your application to Stripe, Inc.", message_id="gm1")],
        message_id_map=DEFAULT_MAP,
    )
    assert len(result.pending) == 1
    assert result.pending[0].pending_reason == "backward_move"


def test_near_duplicate_beats_backward_move_first_guard_wins_across_steps(db):
    """2C ledger Fix 3 (cross-step regression): a proposal that is BOTH a
    near-duplicate entity match (step 2) AND a backward stage move against
    the matched entity's current stage (step 3) must report step 2's reason
    — 'near_duplicate_entity' — not step 3's 'backward_move'. The test above
    (test_backward_move_beats_low_confidence_first_guard_wins) only pins
    'first guard wins' for step 3 vs step 8; this pins it ACROSS steps 2 and
    3, since step 2 runs first and _process_one's step-3 guard is gated
    `if pending_reason is None`."""
    _mk_user(db)
    schema = multi_entity_schema()
    task = _mk_task(db, schema=schema)
    entity = repo.get_or_create_entity(db, task_id=task.id, user_id="u1", entity_key="stripe", display_name="Stripe")
    entity.state = {"stage": "onsite"}
    db.commit()

    # "Stripewise Corp" is a near-duplicate of "stripe" (step 2 routes to
    # pending on the closest existing entity, 'stripe'); the proposed stage
    # move ("applied", earlier than the resolved entity's current "onsite")
    # is ALSO a backward move (step 3) against that same resolved entity.
    result = validate_and_stage(
        db, task=task, schema=schema, parsed=DEFAULT_THREAD, thread_row_id=THREAD_ROW_ID,
        proposals=[_proposal(entity="Stripewise Corp", is_new=True, field="stage", new_value="applied",
                              confidence=95, evidence="received your application to Stripe, Inc.",
                              message_id="gm1")],
        message_id_map=DEFAULT_MAP,
    )
    assert result.applied == []
    assert len(result.pending) == 1
    assert result.pending[0].pending_reason == "near_duplicate_entity"
    assert result.pending[0].proposed_entity == "Stripewise Corp"
