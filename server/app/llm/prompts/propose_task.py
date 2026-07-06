"""Goal -> proposed task draft prompt.

One Sonnet-class call (stage="propose", model=settings.llm_extract_model)
turns a user's free-text goal into everything `workers.task_engine_tasks.
propose_task_draft` needs to build a tracker draft: a short display name, a
relevance description (used the same way a bucket's description is used by
`llm/prompts/score_thread.py` to judge whether an inbox thread belongs), a
freeform `state_schema` dict (this module only shape-checks that a dict came
back -- `app.task_engine.schema.validate_schema` is the actual EPS gate, and
it is the WORKER's job to call it, retry, and fall back), and a handful of
full-text-search `keyword_probes` the worker uses to pre-filter candidate
threads before scoring.

Mirrors `llm/prompts/extract_transition.py` and `score_thread.py`'s
build_user_message/parse_response shape. Unlike those two, this call has no
`task_id` yet -- the task doesn't exist until the user accepts the draft --
so only `user_id` threads through to the LLM metrics row (see the worker).

`build_user_message` embeds the EPS format spec and one worked example
directly in the per-call user message (not the system prompt): the compact
spec is what actually teaches the model to emit a schema `validate_schema`
will accept, and keeping it next to the goal keeps the two prompt-authoring
surfaces (system prompt = role + output contract, user message = this
request's content) doing the job `classify_thread.py`/`extract_transition.py`
already establish for this codebase.
"""

import json

SYSTEM_PROMPT = """You turn a user's goal into a tracker configuration for their inbox.

A tracker watches incoming email threads and reports which ones are relevant
to the goal and what stage each one (or, for multi-entity trackers, each
tracked thing) is at.

Given a goal, output exactly one line of JSON, no other text or code fences:
  {"name": "<short label, <=40 chars>",
   "description": "<1-3 sentences: what this tracks and what makes an email
   thread relevant to it>",
   "state_schema": {... an EPS object, format given in the user message ...},
   "keyword_probes": ["<3-8 short full-text-search terms that would surface
   relevant emails already in the inbox>", ...]}
"""

_EPS_SPEC = """EPS (Entity-Pipeline Schema) format for state_schema:
  version   - always 1
  entity    - null for a task that tracks ONE thing (e.g. "my visa
              application"), or an object {"noun": "<the thing tracked, e.g.
              company>", "identity_hint": "<short hint for telling two
              instances apart>", "attributes": [...]} for a task that tracks
              MANY named instances of the same kind of thing (e.g. every
              company you're interviewing with).
  pipeline  - {"stages": [<ordered non-terminal stage names>],
               "terminal": [<terminal stage names, e.g. done/rejected>]}
              -- at least one non-terminal stage is required; terminal may
              be empty but is almost always at least one stage (done,
              accepted, rejected, ...).
  attributes (entity-only) - each is {"key": "<field name>",
              "type": "string"|"number"|"datetime"|"boolean"|"enum",
              "values": [...]}  ("values" required only, and only, for
              type "enum")."""

_WORKED_EXAMPLE = (
    'Worked example -- goal "help me land a new job":\n'
    '{"name": "Job hunt", "description": "Tracks companies I\'m interviewing '
    "with, from application through offer or rejection. Relevant threads "
    "mention a company I've applied to, recruiter outreach, interview "
    'scheduling, or an offer/rejection decision.", "state_schema": '
    '{"version": 1, "entity": {"noun": "company", "identity_hint": "the '
    'hiring company\'s name", "attributes": [{"key": "role", "type": '
    '"string"}, {"key": "level", "type": "enum", "values": ["junior", "mid", '
    '"senior"]}]}, "pipeline": {"stages": ["applied", "interview", '
    '"onsite"], "terminal": ["offer", "rejected"]}}, "keyword_probes": '
    '["interview scheduled", "recruiter", "job offer", "application '
    'received", "onsite interview"]}'
)


def build_user_message(*, goal: str) -> str:
    return f"{_EPS_SPEC}\n\n{_WORKED_EXAMPLE}\n\nGoal: {goal}"


def _clean_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) else None


def parse_response(text: str) -> dict | None:
    """Shape check only -- EPS validation is the worker's job (via
    `app.task_engine.schema.validate_schema`). Lenient exactly where the
    contract calls for it: a name over 40 chars is truncated rather than
    rejected, and a keyword_probes list over 8 items is clamped to its first
    8 rather than rejected. Anything else that's the wrong shape drops the
    whole response (None) -- there is no per-field recovery for a
    missing/mistyped description or a state_schema that isn't even a dict.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            text = text[s : e + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    name = _clean_str(obj.get("name"))
    if not name:
        return None
    name = name[:40]  # leniently truncate, never drop for length alone

    description = _clean_str(obj.get("description"))
    if description is None:
        return None

    state_schema = obj.get("state_schema")
    if not isinstance(state_schema, dict):
        return None

    probes_raw = obj.get("keyword_probes")
    if not isinstance(probes_raw, list):
        return None
    probes = [p.strip() for p in probes_raw if isinstance(p, str) and p.strip()]
    probes = probes[:8]  # clamp extras; EPS/search fallback handles too-few

    return {
        "name": name,
        "description": description,
        "state_schema": state_schema,
        "keyword_probes": probes,
    }
