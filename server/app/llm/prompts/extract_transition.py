"""Extraction prompt: goal + EPS schema + entity roster + a per-message-marked
thread -> a JSON array of proposed state transitions.

This module only builds the LLM input and shape-checks its output — the pure
8-step validator (`app.task_engine.transitions.validate_and_stage`) is what
actually accepts, defers, or rejects each proposal against the database.
Mirrors `llm/prompts/classify_thread.py` and `score_thread.py`'s
build_user_message/parse_response shape.
"""

import json
from datetime import datetime, timezone

from app.db.models import TaskStateEntity
from app.gmail.parser import ParsedThread
from app.task_engine.schema import TaskStateSchema

SYSTEM_PROMPT = """You extract task-state transitions from an email thread.

The user is tracking progress toward a goal using a pipeline schema: an
ordered list of non-terminal stages plus a set of terminal stages, and (for
multi-entity tasks) a roster of named entities with typed attributes. Read
the thread and decide what, if anything, changed.

Match entities against the roster before deciding something is new — reuse
an existing entity's exact key whenever the thread is clearly about it.

For each real change, emit one object:
  entity          - the entity's existing key from the roster, or (only if
                    genuinely new) its display name verbatim from the thread
  is_new_entity   - true only if this entity is not already in the roster
  field           - "stage", or one of the declared attribute keys
  new_value       - the new value as a plain string (a stage name, or the
                    attribute's value in its natural text form)
  evidence_quote  - a VERBATIM quotation from the thread supporting this
                    change (copy exact wording; never paraphrase)
  message_id      - the gmail message id from the "[message <id> | <date>]"
                    marker the evidence quote comes from
  confidence      - integer 0-100: how confident you are this change is real
                    and correctly attributed

If nothing changed, or you cannot find a verbatim supporting quote, omit
that item entirely — never guess.

Output exactly one JSON array, no other text or code fences:
  [{"entity": "...", "is_new_entity": false, "field": "...", "new_value": "...",
    "evidence_quote": "...", "message_id": "...", "confidence": 80}, ...]
or []
"""


def _render_schema(schema: TaskStateSchema) -> str:
    lines = [
        f"Stages (in order): {', '.join(schema.pipeline.stages)}",
        f"Terminal stages: {', '.join(schema.pipeline.terminal) or '(none)'}",
    ]
    if schema.entity is None:
        lines.append(
            'Entity: singleton — this task tracks one thing, not a roster; '
            'always use entity key "_self".'
        )
    else:
        e = schema.entity
        hint = f" — {e.identity_hint}" if e.identity_hint else ""
        lines.append(f"Entity: {e.noun}{hint}")
        if e.attributes:
            lines.append("Attributes:")
            for a in e.attributes:
                if a.type == "enum":
                    lines.append(f"  - {a.key} (enum: {', '.join(a.values or [])})")
                else:
                    lines.append(f"  - {a.key} ({a.type})")
    return "\n".join(lines)


def _render_roster(schema: TaskStateSchema, entities: list[TaskStateEntity]) -> str:
    if schema.entity is None:
        return 'Current state (match before you create): singleton — always use entity key "_self".'
    if not entities:
        return "Current entities (match before you create): (none yet — every entity below will be new)"
    lines = ["Current entities (match before you create):"]
    for e in entities:
        lines.append(f"  {e.entity_key}: {json.dumps(e.state)}")
    return "\n".join(lines)


def build_user_message(
    *, goal: str, schema: TaskStateSchema, entities: list[TaskStateEntity], thread_str_with_ids: str,
    user_corrections: list | None = None,
) -> str:
    """user_corrections (spec §4.6 learning loop, Task 2): pre-rendered
    one-line strings, one per recent human correction, already resolved by
    the caller (task_engine.engine.extract_for_pair) into the shape
    '{entity display name}: user set {field} to "{new_value}"' — this module
    stays free of db access, so it just prefixes each with "- " and renders
    the section; empty/None omits the section entirely (byte-identical to
    the pre-Task-2 prompt)."""
    corrections_section = ""
    if user_corrections:
        rendered = "\n".join(f"- {line}" for line in user_corrections)
        corrections_section = f"Corrections the user has made (respect these):\n{rendered}\n\n"
    return (
        f"Goal: {goal}\n\n"
        f"Schema:\n{_render_schema(schema)}\n\n"
        f"{_render_roster(schema, entities)}\n\n"
        f"{corrections_section}"
        f"Thread:\n\n{thread_str_with_ids}"
    )


def _parse_one(item: object) -> dict | None:
    """Shape-check one proposal dict against the output contract; None if it
    doesn't match (missing/wrong-typed key, confidence outside 0-100)."""
    if not isinstance(item, dict):
        return None
    entity = item.get("entity")
    is_new_entity = item.get("is_new_entity")
    field = item.get("field")
    new_value = item.get("new_value")
    evidence_quote = item.get("evidence_quote")
    message_id = item.get("message_id")
    confidence = item.get("confidence")

    if not isinstance(entity, str) or not entity.strip():
        return None
    if not isinstance(is_new_entity, bool):
        return None
    if not isinstance(field, str) or not field.strip():
        return None
    if not isinstance(new_value, str):
        return None
    if not isinstance(evidence_quote, str):
        return None
    if not isinstance(message_id, str) or not message_id.strip():
        return None
    # bool is an int subclass in Python; exclude it explicitly so a stray
    # true/false doesn't silently pass as confidence 1/0.
    if isinstance(confidence, bool):
        return None
    if isinstance(confidence, int):
        if not 0 <= confidence <= 100:
            return None
    elif isinstance(confidence, float):
        # A model response occasionally emits a float ("82.5") where the
        # contract asks for an int — round + clamp to 0-100 rather than
        # discarding an otherwise-real transition over a formatting quirk.
        # Only partial parity with triage_thread.parse_response's clamping
        # (float branch only; out-of-range ints still rejected, unlike
        # triage which clamps them).
        confidence = max(0, min(100, int(round(confidence))))
    else:
        return None

    return {
        "entity": entity, "is_new_entity": is_new_entity, "field": field,
        "new_value": new_value, "evidence_quote": evidence_quote,
        "message_id": message_id, "confidence": confidence,
    }


def parse_response(text: str) -> list[dict]:
    """Parse the model's JSON array. Malformed JSON, or a response that
    isn't a JSON array at all, -> []. Individual array items that don't match
    the shape are dropped (not the whole response) — one hallucinated field
    in one transition shouldn't discard the other, well-formed ones."""
    text = (text or "").strip()
    if text.startswith("```"):
        s, e = text.find("["), text.rfind("]")
        if s >= 0 and e > s:
            text = text[s : e + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, list):
        return []
    out: list[dict] = []
    for item in obj:
        parsed = _parse_one(item)
        if parsed is not None:
            out.append(parsed)
    return out


def thread_to_string_with_ids(parsed: ParsedThread) -> str:
    """`thread_to_string`, but each message is preceded by a
    "[message <gmail_message_id> | <iso date>]" marker so extraction output
    can cite exactly which message its evidence came from."""
    lines: list[str] = [f"Thread subject: {parsed.subject or '(no subject)'}"]
    for m in parsed.messages:
        iso = datetime.fromtimestamp(m.gmail_internal_date / 1000, tz=timezone.utc).isoformat()
        lines.append("---")
        lines.append(f"[message {m.gmail_message_id} | {iso}]")
        lines.append(f"From: {m.from_addr or ''}")
        lines.append(f"To: {m.to_addr or ''}")
        lines.append(f"Subject: {m.subject or ''}")
        lines.append("")
        lines.append(m.body_text)
    return "\n".join(lines)
