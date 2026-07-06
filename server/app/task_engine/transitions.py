"""The pure 8-step mechanical validator for LLM-proposed task-state
transitions (spec §4.4 in `specs/004_vision_arch/chosen-architecture.md`).

This module makes every accept/reject/defer-to-review decision deterministic
given (task, schema, parsed thread, proposals) plus whatever is already in
the database — no LLM calls, no redis, no settings read at import time
(`get_settings()` is called inside `validate_and_stage`, matching the rest of
the codebase's settings-are-read-per-call convention). `app/task_engine/
engine.py` (Task 7) is the only caller: it owns the LLM call, builds
`message_id_map`, and commits + publishes after `validate_and_stage` returns.

Entity similarity is pure-Python `difflib.SequenceMatcher` on normalized keys
— a deliberate deviation from the spec's `pg_trgm` mention (see plan's Global
Constraints): dialect-independent and unit-testable on the SQLite `create_all`
test fixture, which has no Postgres extensions at all.
"""

import logging
import re
import string
from dataclasses import dataclass, field
from datetime import timezone
from difflib import SequenceMatcher

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Task, TaskEvent, TaskStateEntity
from app.gmail.parser import ParsedThread, thread_to_string
from app.task_engine import repo
from app.task_engine.schema import SINGLETON_KEY, TaskStateSchema, coerce_value

log = logging.getLogger(__name__)

# Entity-resolution similarity thresholds (step 2, spec §4.4): >= MATCH is an
# auto-match; [PENDING, MATCH) routes to pending_review on the closest
# existing entity instead of minting a likely duplicate; < PENDING either
# creates a new entity (if the LLM flagged one) or drops the proposal.
ENTITY_MATCH_THRESHOLD = 0.6
ENTITY_PENDING_THRESHOLD = 0.4


@dataclass
class StagedResult:
    """Outcome of one `validate_and_stage()` call. Every proposal lands in
    exactly one bucket: applied, pending, or dropped. `dropped` is the
    catch-all for every guard-clause exit that isn't a stored event —
    malformed shape, no entity match, fabricated evidence, a silent no-op, or
    an idempotent replay — so `len(proposals) == len(applied) + len(pending)
    + dropped` always holds for one `validate_and_stage` call."""

    applied: list[TaskEvent] = field(default_factory=list)
    pending: list[TaskEvent] = field(default_factory=list)
    touched_entity_ids: set[str] = field(default_factory=set)
    dropped: int = 0


def normalize_key(name: str) -> str:
    """casefold, strip punctuation, collapse whitespace runs to one space.
    The identity-comparison key for entity resolution (step 2) — e.g.
    "Stripe, Inc." -> "stripe inc"."""
    folded = (name or "").strip().casefold()
    no_punct = folded.translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", no_punct).strip()


def similarity(a: str, b: str) -> float:
    """difflib ratio in [0, 1] — the sole fuzzy-match primitive (no
    pg_trgm; see module docstring)."""
    return SequenceMatcher(None, a, b).ratio()


def _to_ms_epoch(dt) -> int:
    """datetime -> ms since epoch, treating a naive datetime as UTC.

    Every `created_at` this codebase writes is `datetime.now(timezone.utc)`
    (repo.py convention), but SQLite's DATETIME storage (the test fixture's
    dialect) does not round-trip tzinfo — after `expire_on_commit` reloads a
    row, `created_at` comes back naive. A naive datetime's `.timestamp()`
    assumes the *local* system timezone, which would silently corrupt this
    comparison on any machine not set to UTC. Postgres (production) returns
    real tz-aware datetimes for `TIMESTAMPTZ`, so this is a no-op there.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _normalize_whitespace(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip — used for
    the evidence substring check (step 4), which must not be defeated by a
    line-wrap or double-space difference between the quote and the source
    text."""
    return re.sub(r"\s+", " ", text or "").strip()


def _append_event_guarded(db: Session, **kwargs) -> TaskEvent | None:
    """`repo.append_event`, with the insert's flush wrapped in a SAVEPOINT
    (`db.begin_nested()`).

    Step 7's SELECT-first check (`repo.find_event_for_message_field`) is the
    fast path; the partial unique index `uq_task_event_msg_field` (task_id,
    message_id, field) is only the race backstop for a genuinely concurrent
    duplicate insert on a migrated DB — it does not exist in the `create_all`
    test fixture (Base.metadata has no raw-SQL partial index), so this branch
    is unreachable from the test suite by design and is verified by review,
    not by a unit test.

    The SAVEPOINT matters, not just the try/except: on Postgres, a failed
    flush aborts the *entire* enclosing transaction until a rollback (or
    rollback-to-savepoint) happens. A bare `except IntegrityError` without a
    nested transaction would poison every other proposal already flushed
    (but not yet committed) in this same `validate_and_stage` batch — a bug,
    since a caller may pass many proposals per call. `begin_nested()` scopes
    the rollback to just this one insert.
    """
    try:
        with db.begin_nested():
            return repo.append_event(db, **kwargs)
    except IntegrityError:
        log.info(
            "transitions: idempotency race backstop hit task=%s field=%s message_id=%s",
            kwargs.get("task").id, kwargs.get("field"), kwargs.get("message_id"),
        )
        return None


def validate_and_stage(
    db: Session,
    *,
    task: Task,
    schema: TaskStateSchema,
    parsed: ParsedThread,
    thread_row_id: str,
    proposals: list[dict],
    message_id_map: dict[str, str],
) -> StagedResult:
    """Run every proposal through the 8-step guard chain, in order, and
    persist the outcome (applied / pending_review / dropped) via `repo`.
    Never commits — the caller owns the transaction and the publish.

    `message_id_map` maps gmail_message_id -> the internal `InboxMessage.id`
    the engine resolved from the same db rows `parsed` was rebuilt from. This
    is an addition beyond the brief's literal signature: `TaskEvent.message_id`
    must be the INTERNAL id (a soft pointer, per `db/models.py`), never the
    Gmail id, and this module deliberately has no InboxMessage/db access of
    its own (kept pure + repo-only) — so the caller (engine.py, Task 7, which
    already loads the InboxMessage rows to build `parsed`) supplies the map.
    """
    result = StagedResult()
    settings = get_settings()
    thread_text_normalized = _normalize_whitespace(thread_to_string(parsed))
    all_stages = schema.all_stages()
    message_by_gmail_id = {m.gmail_message_id: m for m in parsed.messages}

    # Local roster cache, seeded from the DB and updated as new entities are
    # minted mid-batch so proposal N+1 in this same call can match an entity
    # proposal N just created.
    known_entities: dict[str, TaskStateEntity] = {
        e.entity_key: e for e in repo.list_entities(db, task_id=task.id)
    }

    for proposal in proposals:
        _process_one(
            db, task=task, schema=schema, all_stages=all_stages,
            thread_text_normalized=thread_text_normalized,
            message_by_gmail_id=message_by_gmail_id, message_id_map=message_id_map,
            thread_row_id=thread_row_id, known_entities=known_entities,
            proposal=proposal, settings=settings, result=result,
        )
    return result


def _resolve_entity(
    db: Session, *, task: Task, schema: TaskStateSchema,
    entity_name: str, is_new_entity: bool, known_entities: dict[str, TaskStateEntity],
) -> tuple[TaskStateEntity | None, bool]:
    """Step 2. Returns (entity, forced_pending); entity is None for a hard
    drop (no match, and the LLM didn't flag it as new)."""
    if schema.entity is None:
        entity = known_entities.get(SINGLETON_KEY)
        if entity is None:
            entity = repo.get_or_create_entity(
                db, task_id=task.id, user_id=task.user_id,
                entity_key=SINGLETON_KEY, display_name=task.name,
            )
            known_entities[SINGLETON_KEY] = entity
        return entity, False

    normalized = normalize_key(entity_name)
    exact = known_entities.get(normalized)
    if exact is not None:
        return exact, False

    best_key: str | None = None
    best_score = -1.0
    for key in known_entities:
        score = similarity(normalized, key)
        if score > best_score:
            best_key, best_score = key, score

    if best_key is not None and best_score >= ENTITY_MATCH_THRESHOLD:
        return known_entities[best_key], False
    if best_key is not None and best_score >= ENTITY_PENDING_THRESHOLD:
        # A near-duplicate needs a human — stage on the closest existing
        # entity as pending_review rather than auto-matching or minting a
        # duplicate, regardless of what the LLM's is_new_entity flag says.
        return known_entities[best_key], True
    if is_new_entity:
        entity = repo.get_or_create_entity(
            db, task_id=task.id, user_id=task.user_id,
            entity_key=normalized, display_name=entity_name,
        )
        known_entities[normalized] = entity
        return entity, False
    return None, False


def _process_one(
    db: Session, *, task: Task, schema: TaskStateSchema, all_stages: list[str],
    thread_text_normalized: str, message_by_gmail_id: dict, message_id_map: dict[str, str],
    thread_row_id: str, known_entities: dict[str, TaskStateEntity],
    proposal: dict, settings, result: StagedResult,
) -> None:
    field_name = proposal.get("field")
    raw_new_value = proposal.get("new_value")
    entity_name = proposal.get("entity") or ""
    is_new_entity = bool(proposal.get("is_new_entity"))
    evidence_quote = proposal.get("evidence_quote") or ""
    gmail_message_id = proposal.get("message_id")
    confidence = proposal.get("confidence", 0)

    # --- Step 1: shape -----------------------------------------------------
    # Malformed output is noise, not signal: never applied, never reviewed.
    if field_name == "stage":
        if raw_new_value not in all_stages:
            log.info("transitions: dropped task=%s (stage %r not in schema)", task.id, raw_new_value)
            result.dropped += 1
            return
        new_value = raw_new_value
    else:
        attr = schema.attr(field_name)
        if attr is None:
            log.info("transitions: dropped task=%s (unknown field %r)", task.id, field_name)
            result.dropped += 1
            return
        try:
            new_value = coerce_value(attr.type, raw_new_value, enum_values=attr.values)
        except (ValueError, TypeError) as exc:
            log.info("transitions: dropped task=%s (field %r value %r coercion failed: %s)",
                      task.id, field_name, raw_new_value, exc)
            result.dropped += 1
            return

    # --- Step 2: entity resolution ------------------------------------------
    entity, forced_pending = _resolve_entity(
        db, task=task, schema=schema, entity_name=entity_name,
        is_new_entity=is_new_entity, known_entities=known_entities,
    )
    if entity is None:
        log.info("transitions: dropped task=%s (no entity match for %r)", task.id, entity_name)
        result.dropped += 1
        return

    # --- Step 3: stage legality ----------------------------------------------
    # Only users move terminal entities; LLM backward moves always go to review.
    if field_name == "stage":
        current_stage = entity.state.get("stage")
        if current_stage in schema.pipeline.terminal:
            forced_pending = True
        elif (
            current_stage in all_stages and new_value in all_stages
            and all_stages.index(new_value) < all_stages.index(current_stage)
        ):
            forced_pending = True

    # --- Step 4: evidence ------------------------------------------------------
    # Fail closed: no verbatim quote in the thread, no write at all (not even
    # pending_review) — the cheapest hallucination guard.
    normalized_quote = _normalize_whitespace(evidence_quote)
    if not normalized_quote or normalized_quote not in thread_text_normalized:
        log.info("transitions: dropped task=%s entity=%s field=%s (evidence not found)",
                  task.id, entity.entity_key, field_name)
        result.dropped += 1
        return

    # --- Step 5: correction fences -----------------------------------------------
    # After a user corrects this entity (any field), only evidence strictly
    # newer than that correction may move it again.
    fence_event = repo.latest_applied_user_event(db, entity_id=entity.id)
    if fence_event is not None:
        message = message_by_gmail_id.get(gmail_message_id)
        fence_ms = _to_ms_epoch(fence_event.created_at)
        if message is None or not (message.gmail_internal_date > fence_ms):
            forced_pending = True

    # --- Step 6: no-op ----------------------------------------------------------
    if entity.state.get(field_name) == new_value:
        result.dropped += 1
        return

    # --- Step 7: idempotency -----------------------------------------------------
    internal_message_id = message_id_map.get(gmail_message_id)
    if repo.find_event_for_message_field(
        db, task_id=task.id, message_id=internal_message_id, field=field_name,
    ) is not None:
        result.dropped += 1
        return

    # --- Step 8: confidence gate --------------------------------------------------
    old_value = entity.state.get(field_name)
    event_kwargs = dict(
        task=task, entity=entity, origin="llm",
        field=field_name, old_value=old_value, new_value=new_value,
        evidence_quote=evidence_quote, confidence=confidence,
        thread_id=thread_row_id, message_id=internal_message_id,
        gmail_message_id=gmail_message_id,
    )
    if not forced_pending and confidence >= settings.task_apply_confidence:
        event = _append_event_guarded(db, status="applied", **event_kwargs)
        if event is None:
            result.dropped += 1
            return
        repo.apply_event(db, task=task, entity=entity, event=event)
        result.applied.append(event)
    else:
        event = _append_event_guarded(db, status="pending_review", **event_kwargs)
        if event is None:
            result.dropped += 1
            return
        result.pending.append(event)

    result.touched_entity_ids.add(entity.id)
