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
