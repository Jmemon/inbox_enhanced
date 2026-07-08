"""Task-engine orchestration: the single extraction call for one (task,
thread) pair.

This module owns the one thing neither `transitions.py` nor `repo.py` may
own: the LLM round-trip. It rebuilds the thread from stored Postgres rows
(never refetches Gmail — same self-sufficiency rule `_score_all` and
backfill_task's own `load_parsed_threads` calls already follow), builds
the `gmail_message_id -> internal
InboxMessage.id` map from the SAME rows `inbox_repo.load_parsed_threads` used
internally, calls the extraction LLM once, and hands `extract_transition.
parse_response(...)`'s output straight to `transitions.validate_and_stage` —
never a hand-built proposal dict, per that module's documented contract
("trust nothing" is the validator's job; feeding it anything but the parser's
own output would defeat the point of that contract).

Caller (Task 8's `workers/task_engine_tasks.py`) owns the transaction commit
and the realtime publish — this module only flushes (via validate_and_stage/
repo) so the caller stays in charge of when a database write actually lands.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import InboxMessage, Task
from app.inbox import inbox_repo
from app.llm import client as llm_client
from app.llm.prompts import extract_transition
from app.task_engine import repo
from app.task_engine import schema as schema_mod
from app.task_engine.transitions import StagedResult, validate_and_stage

log = logging.getLogger(__name__)


def extract_for_pair(
    db: Session, *, task: Task, thread_internal_id: str, user_id: str,
) -> StagedResult | None:
    """Run one extraction LLM call for (task, thread) and validate + stage
    its output. Returns None when there's nothing to extract from: the
    thread has no usable parsed rows (e.g. every message soft-deleted, or the
    internal id doesn't belong to this user), or the task has no schema yet
    (schema-less tasks are classify-only — extraction is Task 6+'s job, and
    `list_active_trackers` already filters these out for the normal
    `process_task_updates` caller; this is a defensive guard for any other
    caller, e.g. the single-pair `extract_for_thread` variant)."""
    if task.state_schema is None:
        log.info("extract_for_pair: task=%s has no state_schema, skipping", task.id)
        return None

    triples = inbox_repo.load_parsed_threads(
        db, user_id=user_id, internal_ids=[thread_internal_id],
    )
    if not triples:
        log.info(
            "extract_for_pair: task=%s thread=%s has no usable parsed rows, skipping",
            task.id, thread_internal_id,
        )
        return None
    _, _, parsed = triples[0]

    schema = schema_mod.validate_schema(task.state_schema)

    # message_id_map: gmail_message_id -> internal InboxMessage.id, built from
    # the SAME (thread_id, not-deleted) rows load_parsed_threads' own query
    # selects — transitions.py deliberately has no InboxMessage access of its
    # own (kept pure + repo-only), so this is the one place that map can be
    # built correctly.
    message_rows = db.execute(
        select(InboxMessage).where(
            InboxMessage.thread_id == thread_internal_id,
            InboxMessage.is_deleted == False,  # noqa: E712
        )
    ).scalars().all()
    message_id_map = {m.gmail_id: m.id for m in message_rows}

    entities = repo.list_entities(db, task_id=task.id)
    settings = get_settings()
    thread_str_with_ids = extract_transition.thread_to_string_with_ids(parsed)

    # spec §4.6 learning loop (Task 2): surface the user's most recent
    # explicit corrections (manual state edits — the same origin='user'/
    # status='applied' events `latest_applied_user_event`'s fence already
    # tracks) directly in the extraction prompt, so the model is discouraged
    # from proposing a change that would relitigate one, not just fenced out
    # after the fact. Entity display names are resolved here (from `entities`,
    # already loaded above) rather than in the prompt module, which stays
    # free of db access per its own docstring.
    entity_display_by_id = {e.id: e.display_name for e in entities}
    recent_events = repo.recent_user_events(db, task_id=task.id)
    user_corrections = [
        f'{entity_display_by_id.get(ev.entity_id, "unknown")}: user set {ev.field} to "{ev.new_value}"'
        for ev in recent_events
    ]

    user_message = extract_transition.build_user_message(
        goal=task.goal, schema=schema, entities=entities,
        thread_str_with_ids=thread_str_with_ids,
        user_corrections=user_corrections,
    )

    text = llm_client.run_in_loop(
        llm_client.call_messages(
            model=settings.llm_extract_model,
            system=extract_transition.SYSTEM_PROMPT,
            user=user_message,
            stage="extract",
            user_id=user_id,
            task_id=task.id,
        )
    )
    proposals = extract_transition.parse_response(text)

    return validate_and_stage(
        db, task=task, schema=schema, parsed=parsed, thread_row_id=thread_internal_id,
        proposals=proposals, message_id_map=message_id_map,
    )
