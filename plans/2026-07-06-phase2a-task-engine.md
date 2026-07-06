# Phase 2A — Task Engine Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Spec: `specs/004_vision_arch/chosen-architecture.md` §3 Phase 2 + §4 (task engine core) + §6.
> Phase 2 is split: **2A (this plan) = everything server-side** — migration, EPS, engine, pipeline, API. **2B (next plan) = task UI** (wizard, board, corrections), written once 2A's API is real. 2A is independently shippable: every endpoint and pipeline stage is pytest-verifiable.
> Plan stamped at commit `df9da00` on branch `main`.

**Goal:** The tracker engine end to end: task tables, the EPS schema language + mechanical validator, a single triage LLM call replacing classify (bucket pick + tracker relevance, dual-write), a decoupled Sonnet-class extraction pipeline with confidence gating and correction fences, the append-only `task_events` ledger with refold-on-revert, task CRUD + draft-proposal + backfill + correction API, and `task_*` SSE events.

**Architecture:** New module `server/app/task_engine/` (NOT `app/tasks/` — collides with `workers/tasks.py`): `schema.py` (EPS pydantic), `repo.py` (never commits), `criteria.py` (moved `formulate_criteria`), `transitions.py` (pure validator), `engine.py` (orchestration), `draft_cache.py` (redis, `preview_cache` pattern). Prompts in `llm/prompts/`: `triage_thread.py`, `extract_transition.py`, `propose_task.py`. Celery tasks in `workers/task_engine_tasks.py`. Router `api/tasks.py`. Triage runs where classify runs today (inside sync); extraction is a decoupled follow-up Celery task holding no `sync_lock` (idempotency via a partial unique index).

**Tech Stack:** Python 3.13 / SQLAlchemy 2 / Alembic / Celery / FastAPI / OpenRouter (Haiku triage, Sonnet extraction).

## Global Constraints

- `uv` only; server commands from `server/`; pipe test output (`2>&1 | tail -5`); NEVER read `.env` (update `.env.example` for new vars).
- Tests run on SQLite in-memory + fakeredis. NO Postgres-only constructs in code paths tests exercise: JSON columns use `sa.JSON().with_variant(JSONB, "postgresql")`; the events partial unique index uses standard `CREATE UNIQUE INDEX … WHERE` (valid on both dialects); **entity similarity uses pure-Python `difflib.SequenceMatcher`** (deviation from the spec's pg_trgm mention, same 0.6 / 0.4–0.6 thresholds — dialect-independent and unit-testable; note it in the reference docs).
- Repo/engine functions NEVER commit (caller owns txn); production READ helpers never flush; write helpers may; tests flush after ORM mutations.
- Publish-after-commit for every SSE event; events carry ids/versions, never rows.
- Every LLM call goes through `llm/client.call_messages` with `stage=` and `user_id=` (and, new, `task_id=`) so `llm_calls` rollups stay complete.
- Confidence scales are 0–100 everywhere. Settings (all in `config.py` + `.env.example`): `LLM_EXTRACT_MODEL` (default `anthropic/claude-sonnet-4.5`), `TASK_APPLY_CONFIDENCE` (default 75), `TASK_LINK_CONFIDENCE` (default 60).
- Sticky user links are inviolable: no LLM path may modify a `task_thread_links` row whose `origin='user'`.
- `tasks.version` bumps on every applied/reverted/approved event and every correction; SSE `task_updated` carries `{task_id, version, pending_count}`.
- Buckets are untouched this phase (D2): the `buckets` table, `/api/buckets`, and `inbox_threads.bucket_id` keep working; triage merely replaces the classify CALL.
- Commit per task, `type(scope): summary`, no attribution lines.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/app/db/models.py` | modify | `Task`, `TaskThreadLink`, `TaskStateEntity`, `TaskEvent` |
| `server/migrations/versions/0007_tasks.py` | create | four tables + partial unique index |
| `server/app/task_engine/schema.py` | create | EPS pydantic models + `validate_schema` |
| `server/app/task_engine/repo.py` | create | task/link/entity/event CRUD, `refold_entity`, board/event queries |
| `server/app/task_engine/criteria.py` | create | `formulate_criteria` moved verbatim (bucket_repo re-exports) |
| `server/app/task_engine/transitions.py` | create | pure 8-step validator + apply |
| `server/app/task_engine/engine.py` | create | extraction orchestration (LLM calls + validate + persist) |
| `server/app/task_engine/draft_cache.py` | create | `task_draft:{id}` redis cache (preview_cache pattern) |
| `server/app/llm/prompts/triage_thread.py` | create | single-call bucket pick + tracker relevance |
| `server/app/llm/prompts/extract_transition.py` | create | EPS-constrained transition extraction |
| `server/app/llm/prompts/propose_task.py` | create | goal → {name, description, state_schema, keyword_probes} |
| `server/app/llm/client.py` | modify | `task_id=` kwarg → metrics |
| `server/app/config.py` | modify | three new settings |
| `server/app/workers/gmail_sync.py` | modify | `_classify_batch` → `_triage_batch` (dual-write) |
| `server/app/workers/tasks.py` | modify | sync tasks enqueue `process_task_updates` after publish |
| `server/app/workers/task_engine_tasks.py` | create | `process_task_updates`, `extract_for_thread`, `propose_task_draft`, `backfill_task` |
| `server/app/api/tasks.py` | create | draft/CRUD/board/events/corrections router |
| `server/app/main.py` | modify | register tasks router |
| `reference/*` | modify (final task) | new `TASKS_INDEX.md` + updates + stamps |

Deliberately NOT in 2A: any client change (2B), bucket unification (Phase 4), actions (Phase 5).

---

### Task 1: Migration 0007 + ORM models

**Files:** Modify `server/app/db/models.py`; Create `server/migrations/versions/0007_tasks.py`; Test `server/tests/test_migration_0007.py`.

**Interfaces produced (exact — later tasks depend on these):**

```python
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # null user_id reserved for Phase-4 default classify-tasks; always set in 2A.
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="tracker")  # 'tracker' | 'bucket' (Phase 4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")  # formulate_criteria grammar
    state_schema: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))  # EPS; null = classify-only
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | paused
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)  # SSE gap detection (D4)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class TaskThreadLink(Base):
    __tablename__ = "task_thread_links"
    __table_args__ = (UniqueConstraint("task_id", "thread_id", name="uq_task_thread"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("inbox_threads.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(8), nullable=False)   # 'llm' | 'user' — user rows are sticky
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="attached")  # attached | detached
    confidence: Mapped[int | None] = mapped_column(Integer)          # 0-100 at link time (llm origin)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class TaskStateEntity(Base):
    __tablename__ = "task_state_entities"
    __table_args__ = (UniqueConstraint("task_id", "entity_key", name="uq_task_entity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)  # normalized ('stripe'); '_self' for singleton
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # {"stage": str|None, "<attr key>": value, ...} — ALWAYS derivable as a fold
    # over applied task_events; refold_entity() rebuilds after revert/reject.
    state: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)  # soft ptr — events outlive merges
    thread_id: Mapped[str | None] = mapped_column(String(36))              # soft ptr (provenance)
    message_id: Mapped[str | None] = mapped_column(String(36))             # soft ptr; null for user edits
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))       # denormalized — audit survives churn
    field: Mapped[str | None] = mapped_column(String(64))                  # 'stage' or attribute key
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)                # 0-100
    origin: Mapped[str] = mapped_column(String(8), nullable=False)         # 'llm' | 'user'
    status: Mapped[str] = mapped_column(String(16), nullable=False)        # applied|pending_review|rejected|reverted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
```

Migration `0007_tasks` (`down_revision = "0006_data_floor"`): the four tables mirroring the models, plus (raw SQL, valid on BOTH dialects):

```python
op.execute("""
    CREATE UNIQUE INDEX uq_task_event_msg_field ON task_events (task_id, message_id, field)
    WHERE message_id IS NOT NULL
""")
```

Downgrade drops the index + four tables (events → entities → links → tasks, FK order).

**Steps:**
- [ ] Failing test `test_migration_0007.py` (copy `test_migration_0006.py`'s `_alembic_cfg` pattern): upgrade head → four tables exist with the key columns; a second insert of `(task_id, message_id, field)` with non-null message_id raises IntegrityError while two NULL-message_id rows coexist; downgrade to `0006_data_floor` removes all four tables.
- [ ] Run → fail. Add models + migration. Run → pass. Full suite green (`uv run pytest -q 2>&1 | tail -3`).
- [ ] Commit: `feat(db): migration 0007 — task engine tables`

---

### Task 2: EPS schema language (`task_engine/schema.py`)

**Files:** Create `server/app/task_engine/schema.py` (+ empty `server/app/task_engine/__init__.py`); Test `server/tests/test_eps_schema.py`.

**Interfaces produced:**

```python
ATTR_TYPES = {"string", "number", "datetime", "boolean", "enum"}
RESERVED_FIELD = "stage"
SINGLETON_KEY = "_self"

class AttributeSpec(BaseModel):
    key: str                      # non-empty, != RESERVED_FIELD, unique within entity
    type: str                     # ∈ ATTR_TYPES
    values: list[str] | None = None  # required iff type == "enum"

class EntitySpec(BaseModel):
    noun: str                     # non-empty, e.g. "company"
    identity_hint: str = ""
    attributes: list[AttributeSpec] = []

class PipelineSpec(BaseModel):
    stages: list[str]             # min 1, non-empty strings, unique
    terminal: list[str] = []      # unique, disjoint from stages

class TaskStateSchema(BaseModel):
    version: int = 1              # must equal 1 in v1
    entity: EntitySpec | None = None   # None → singleton task (one implicit '_self' entity)
    pipeline: PipelineSpec

    def all_stages(self) -> list[str]: ...     # stages + terminal
    def attr(self, key: str) -> AttributeSpec | None: ...

def validate_schema(raw: dict) -> TaskStateSchema:
    """Pydantic parse + the cross-field rules above. Raises ValueError with a
    human-readable message (fed back to the LLM on retry in propose flow)."""

def coerce_value(spec_type: str, value: str, *, enum_values: list[str] | None = None) -> str:
    """Validate + normalize a raw string for an attribute type. datetime → ISO-8601
    (raises ValueError if unparseable); number → str(float/int); boolean →
    'true'/'false'; enum → exact member; string → stripped. Returns the
    normalized string stored in state / task_events.new_value."""
```

Fixed rules encoded here (not flags): forward/lateral/skip stage moves apply freely; LLM backward moves and terminal-stage exits are the VALIDATOR's job (Task 6), not schema flags. Additive-only edit check: `assert_additive_change(old: TaskStateSchema, new: TaskStateSchema) -> None` raises ValueError naming the removed stage/attribute (used by PATCH; renames are not supported in v1 — remove+add is rejected).

**Steps:**
- [ ] Failing tests: valid job-hunt schema parses; singleton (`entity=None`) parses; duplicate stage rejected; terminal∩stages rejected; attr key `"stage"` rejected; enum without values rejected; `coerce_value` happy/sad paths per type; `assert_additive_change` accepts added stage/attr, rejects removed stage.
- [ ] Implement → pass. Commit: `feat(task-engine): EPS schema language + validator`

---

### Task 3: Task repo (`task_engine/repo.py`) + criteria move

**Files:** Create `server/app/task_engine/repo.py`, `server/app/task_engine/criteria.py`; Modify `server/app/inbox/bucket_repo.py` (re-export); Test `server/tests/test_task_repo.py`.

**Interfaces produced (all take `db: Session`, never commit; ids are uuid4 hex; timestamps `datetime.now(timezone.utc)`):**

```python
# criteria.py — bucket_repo.formulate_criteria moved VERBATIM (docstring included).
# bucket_repo.py keeps: `from app.task_engine.criteria import formulate_criteria  # noqa: F401`
# so api/buckets.py and existing tests keep working untouched (Phase 4 removes the shim).

# repo.py
def create_task(db, *, user_id, name, goal, criteria, state_schema: dict | None, kind="tracker") -> Task
def get_owned_task(db, *, user_id, task_id) -> Task | None          # excludes is_deleted
def list_tasks(db, *, user_id, kind: str | None = None) -> list[Task]   # active+paused, not deleted, name asc
def list_active_trackers(db, *, user_id) -> list[Task]              # kind='tracker', status='active', has state_schema
def bump_version(db, *, task: Task) -> int                          # += 1, returns new

def upsert_link(db, *, task_id, thread_id, user_id, origin, state="attached",
                confidence=None) -> TaskThreadLink | None
    # SELECT by (task_id, thread_id). Existing row with origin='user' and
    # origin arg 'llm' → return None UNCHANGED (sticky — the one inviolable rule).
    # Otherwise insert or update state/confidence/origin/updated_at.
def list_attached_thread_ids(db, *, task_id) -> set[str]
def get_link(db, *, task_id, thread_id) -> TaskThreadLink | None

def get_or_create_entity(db, *, task_id, user_id, entity_key, display_name) -> TaskStateEntity
def list_entities(db, *, task_id) -> list[TaskStateEntity]           # the board; updated_at desc
def append_event(db, *, task, entity: TaskStateEntity | None, origin, status,
                 field=None, old_value=None, new_value=None, evidence_quote=None,
                 confidence=None, thread_id=None, message_id=None,
                 gmail_message_id=None) -> TaskEvent                 # flushes; does NOT apply
def apply_event(db, *, task, entity, event) -> None
    # event.status='applied'; entity.state[event.field]=event.new_value;
    # entity.updated_at=now; bump_version(task)
def list_events(db, *, task_id, status: str | None = None, entity_id: str | None = None,
                limit=50, offset=0) -> list[TaskEvent]               # created_at desc
def pending_count(db, *, task_id) -> int

def refold_entity(db, *, task, entity) -> None
    """Rebuild entity.state as a fold over its APPLIED events ascending
    (created_at, then origin: 'llm' before 'user' so user wins created_at ties).
    Fields with no surviving applied event are removed; 'stage' falls back to
    None. Bumps version. Used by revert/reject/detach/merge."""
```

**Steps:**
- [ ] Failing tests: create/list/get scoping (other-user task invisible); sticky link — `upsert_link(origin='llm')` over a user row returns None and changes nothing, user over llm updates; `uq_task_thread` upsert idempotency; append+apply updates entity state and bumps version; `refold_entity` — apply 3 events on one field, revert the middle (status flip by test), refold → state reflects survivors, user-origin same-timestamp event wins; criteria re-export: `bucket_repo.formulate_criteria is criteria.formulate_criteria` and existing `test_bucket_repo.py` stays green.
- [ ] Implement → pass. Full suite green. Commit: `feat(task-engine): repo + criteria relocation`

---

### Task 4: Settings + LLM client `task_id` threading

**Files:** Modify `server/app/config.py`, `.env.example`, `server/app/llm/client.py`, `server/app/llm/metrics.py` (no change needed if `task_id` param already exists — verify), tests `server/tests/test_llm_client.py`.

- [ ] `config.py` additions (with comments in the file's style): `llm_extract_model: str = Field(default="anthropic/claude-sonnet-4.5", alias="LLM_EXTRACT_MODEL")`, `task_apply_confidence: int = Field(default=75, alias="TASK_APPLY_CONFIDENCE")`, `task_link_confidence: int = Field(default=60, alias="TASK_LINK_CONFIDENCE")`. Mirror all three into `.env.example` with comments.
- [ ] `call_messages` gains `task_id: str | None = None`, passed into BOTH metrics.record_call sites (`metrics.record_call` already has the `task_id` param from Phase 0 — verify, don't re-add).
- [ ] Failing test first: extend the existing success-metrics test to pass `task_id="t1"` and assert it lands in the captured record kwargs.
- [ ] Commit: `feat(llm): extraction model + confidence settings; task_id metrics threading`

---

### Task 5: Triage prompt + dual-write in sync (`_classify_batch` → `_triage_batch`)

**Files:** Create `server/app/llm/prompts/triage_thread.py`; Modify `server/app/llm/classify.py` (new `triage()` alongside `classify()`), `server/app/workers/gmail_sync.py`; Test `server/tests/test_triage.py`, extend `server/tests/test_partial_sync.py`.

**Contract:** ONE Haiku call per thread returns bucket pick AND tracker relevance (D2 — no doubled LLM volume):

```python
# triage_thread.py
SYSTEM_PROMPT = <classify_thread's discipline, extended>:
#  output: {"bucket_name": "<name>"|null, "relevant_tasks": [{"name": "<task name>", "confidence": 0-100}]}
#  buckets rendered exactly as classify_thread renders them (criteria blocks + stability hint);
#  trackers rendered as <task name="...">{criteria}</task> blocks; relevance is
#  multi-label (a thread may feed several trackers); confidence 0-100.
def build_user_message(*, thread_str, buckets, trackers: list[Task], current_bucket_name) -> str
def parse_response(text, buckets, trackers) -> tuple[str | None, list[tuple[str, int]]]
    # (bucket_id|None, [(task_id, confidence), ...]) — name→id resolution with
    # classify_thread.parse_response's exact discipline (unknown/dup names dropped;
    # malformed JSON → (None, [])). Confidences clamped to 0-100 ints.
```

```python
# classify.py — NEW function; classify() stays for callers that still use it
def triage(threads, buckets, trackers, current_bucket_ids, *, user_id=None)
      -> list[tuple[str | None, list[tuple[str, int]]]]
    # same asyncio.gather shape as classify(); stage="classify" (it IS the classify
    # call — one llm_calls row per thread, same cost as before); model = llm_classify_model.
    # No-fit bucket falls back to current_bucket_id exactly like classify().
```

`gmail_sync._classify_batch` becomes `_triage_batch(db, *, user_id, parsed_list) -> list[tuple[str | None, list[tuple[str, int]]]]`: loads buckets (unchanged) + `task_engine.repo.list_active_trackers`; when there are NO active trackers it must produce results identical to today's classify path (regression guarantee). Callers (`partial_sync_inbox`, `full_sync_inbox`, `extend_inbox_history`) unpack `bucket_id` for the upsert as today, then after upserting each thread write links: for each `(task_id, conf)` with `conf >= settings.task_link_confidence`, `repo.upsert_link(..., origin='llm', state='attached', confidence=conf)` (sticky rule inside upsert_link protects user rows; links commit in the same sync transaction). `_reclassify_all` in `workers/tasks.py` switches to `triage()` the same way (bucket writes unchanged, link upserts added).

**Steps:**
- [ ] Failing tests: `triage_thread.parse_response` happy path / null bucket / unknown task name dropped / malformed JSON / confidence clamping; `_triage_batch` with zero trackers ≡ old classify behavior (reuse an existing classify-path test, stub `classify.triage`); partial sync with one tracker → link row written with confidence, below-threshold match not linked, existing `origin='user'` detached link NOT overwritten (seed it, assert unchanged).
- [ ] Implement → pass. Full suite green (existing classify tests must still pass — `classify()` untouched).
- [ ] Commit: `feat(triage): single-call bucket pick + tracker relevance, dual-write links`

---

### Task 6: Extraction prompt + pure validator (`transitions.py`)

**Files:** Create `server/app/llm/prompts/extract_transition.py`, `server/app/task_engine/transitions.py`; Test `server/tests/test_extract_prompt.py`, `server/tests/test_transitions.py` (the exhaustive one).

**Extraction prompt contract:**

```python
# extract_transition.py
def build_user_message(*, goal, schema: TaskStateSchema, entities: list[TaskStateEntity],
                       thread_str_with_ids: str) -> str
    # renders: goal; the schema (stages+terminal, attributes with types/enum values,
    # entity noun + identity_hint); the roster as "entity_key: {state}" lines
    # ("match before you create"); the thread with per-message markers
    # "[message <gmail_message_id> | <internal_date_iso>]" so evidence is attributable.
    # Output contract (SYSTEM_PROMPT): a JSON array (possibly []) of
    # {"entity": "<existing key or new display name>", "is_new_entity": bool,
    #  "field": "stage"|<attr key>, "new_value": "<str>",
    #  "evidence_quote": "<verbatim>", "message_id": "<gmail message id>",
    #  "confidence": 0-100}
def parse_response(text) -> list[dict]   # shape-checked dicts; malformed → []
def thread_to_string_with_ids(parsed: ParsedThread) -> str   # thread_to_string + markers
```

**Validator — `transitions.py`, PURE decisions (no LLM, no redis; db only for reads/writes via repo):**

```python
@dataclass
class StagedResult:
    applied: list[TaskEvent]; pending: list[TaskEvent]
    touched_entity_ids: set[str]; dropped: int

def normalize_key(name: str) -> str      # casefold, strip punctuation/whitespace runs
def similarity(a: str, b: str) -> float  # difflib.SequenceMatcher(None, a, b).ratio()

def validate_and_stage(db, *, task: Task, schema: TaskStateSchema,
                       parsed: ParsedThread, thread_row_id: str,
                       proposals: list[dict]) -> StagedResult
```

The 8 steps, in order, per proposal (spec §4.4 — each is one guard clause with a log line on drop):

1. **Shape**: `field` == `"stage"` or a declared attribute; `new_value` coerces via `schema.py`'s module-level `coerce_value(...)` (stage values checked against `schema.all_stages()` membership instead). Fail → `dropped += 1` (malformed output is noise, not signal).
2. **Entity resolution**: singleton schema → entity_key `_self` always. Else `normalize_key(proposal["entity"])`; exact match on existing keys wins; else best `similarity` ≥ 0.6 → match; 0.4–0.6 → stage as `pending_review` on the CLOSEST existing entity (a near-duplicate needs a human); < 0.4 + `is_new_entity` → create via `get_or_create_entity` (display_name = proposal's entity verbatim); < 0.4 without `is_new_entity` → drop.
3. **Stage legality**: for `field=="stage"` — entity currently in a terminal stage → `pending_review` (only users move terminal entities); target earlier in `stages` order than current (backward) → `pending_review` (real life regresses; a human confirms).
4. **Evidence**: whitespace-normalized `proposal["evidence_quote"]` must be a substring of whitespace-normalized thread text → else drop (fail closed; the cheapest hallucination guard).
5. **Fences**: latest `origin='user'` event for this entity (any field) — if it exists, the proposal's message `gmail_internal_date` must be strictly newer than that event's `created_at`; else `pending_review`.
6. **No-op**: `new_value == entity.state.get(field)` → skip silently.
7. **Idempotency**: an event already exists for `(task_id, message_id, field)` (SELECT first; the partial unique index is the race backstop — wrap the insert's flush in try/except IntegrityError → skip).
8. **Confidence gate**: `confidence >= settings.task_apply_confidence` → `append_event` + `apply_event` (applied); else `append_event(status='pending_review')` (stored, state untouched).

Every event row carries `thread_id=thread_row_id`, `message_id` (internal id resolved from the gmail message id via the parsed thread ↔ db rows — engine passes a mapping), `gmail_message_id`, `evidence_quote`, `confidence`, `origin='llm'`.

**Steps:**
- [ ] Failing tests — the exhaustive validator suite (pure; build proposals as dicts, stub nothing): unknown field dropped; bad enum/datetime dropped; stage not in schema dropped; singleton routes to `_self`; exact entity match; fuzzy ≥0.6 match ("Stripe, Inc." → "stripe"); 0.4–0.6 → pending on closest; new entity created; backward move → pending; terminal locked → pending; fabricated evidence dropped; fence blocks older-message proposal → pending, newer passes; no-op skipped; duplicate (task,message,field) skipped on re-run; ≥75 applied + entity state updated + version bumped; <75 pending + state untouched. Plus `extract_transition.parse_response` shape tests and `thread_to_string_with_ids` marker test.
- [ ] Implement → pass. Commit: `feat(task-engine): extraction prompt + 8-step mechanical validator`

---

### Task 7: Engine orchestration + decoupled Celery extraction

**Files:** Create `server/app/task_engine/engine.py`, `server/app/workers/task_engine_tasks.py`; Modify `server/app/workers/tasks.py` (enqueue hook), `server/app/workers/celery_app.py` (include new module); Test `server/tests/test_task_engine_tasks.py`.

**Interfaces:**

```python
# engine.py
def extract_for_pair(db, *, task: Task, thread_internal_id: str, user_id: str) -> StagedResult | None
    # load_parsed_threads(db, user_id=..., internal_ids=[thread_internal_id]) → parsed;
    # None if no rows. Builds gmail_message_id → internal message id map from db rows.
    # ONE call_messages(model=settings.llm_extract_model, stage="extract",
    # user_id=..., task_id=task.id) via llm_client.run_in_loop; parse; validate_and_stage.
    # Caller commits + publishes.

# task_engine_tasks.py  (module-level SessionLocal seam like workers/tasks.py)
@celery_app.task(name="app.workers.task_engine_tasks.process_task_updates")
def process_task_updates(user_id: str, thread_ids: list[str]) -> None
    # For each active tracker (list_active_trackers): pairs = attached links ∩ thread_ids
    # (links whose state='attached'); for each pair sequentially: extract_for_pair →
    # db.commit() → collect. ONE publish per task at the end:
    # _publish(user_id, "task_updated", {"task_id", "version", "pending_count"}).
    # NO sync_lock (never writes inbox_*; idempotency = partial unique index).

@celery_app.task(name="app.workers.task_engine_tasks.extract_for_thread")
def extract_for_thread(user_id: str, task_id: str, thread_id: str) -> None
    # single-pair variant used by user attach (Task 10). Commit + publish task_updated.
```

Hook in `workers/tasks.py`: `poll_new_messages` (both partial and full paths), `full_sync_inbox_task`, and `reclassify_user_inbox` gain, immediately after their `_publish_thread_ids(...)` call: `task_engine_tasks.process_task_updates.apply_async(args=[user_id, ids], countdown=0)` — guarded by `if ids:`. (Import at module top; `extend_inbox_history_task` does NOT enqueue — extends pull old mail; backfill covers history.)

**Steps:**
- [ ] Failing tests (eager celery + monkeypatched `llm_client.call_messages` returning canned extraction JSON; fakeredis): end-to-end — seed user/thread with body + tracker with schema + attached link; run `process_task_updates` → applied event exists, entity on board, version bumped, ONE `task_updated` publish captured (monkeypatch `_publish`); a thread linked `state='detached'` is skipped; a paused task is skipped; low-confidence canned response → pending_review + pending_count in payload; re-run same input → no duplicate events (idempotent), no spurious publish of new event ids.
- [ ] Implement → pass. Full suite green. Commit: `feat(task-engine): decoupled extraction pipeline + sync enqueue hook`

---

### Task 8: Propose-task draft flow (worker + cache)

**Files:** Create `server/app/llm/prompts/propose_task.py`, `server/app/task_engine/draft_cache.py`; Add `propose_task_draft` to `server/app/workers/task_engine_tasks.py`; Test `server/tests/test_propose_task.py`.

**Contracts:**

```python
# propose_task.py — one Sonnet-class call (stage="propose", model=llm_extract_model)
# goal in → single-line JSON out:
# {"name": "<≤40 chars>", "description": "<relevance description paragraph>",
#  "state_schema": <EPS dict>, "keyword_probes": ["<3-8 FTS search terms>", ...]}
def build_user_message(*, goal: str) -> str   # includes a compact EPS format spec + one worked example (job hunt)
def parse_response(text) -> dict | None       # shape check only; schema validation is the worker's job

# draft_cache.py — preview_cache pattern verbatim, key prefix "task_draft", TTL 600:
def mark_pending(draft_id, *, user_id) / store_result(draft_id, *, user_id, payload: dict) / load(draft_id)

# task_engine_tasks.py
@celery_app.task(name="app.workers.task_engine_tasks.propose_task_draft")
def propose_task_draft(user_id: str, draft_id: str, goal: str) -> None
    # 1. LLM propose (above). validate_schema(raw); on ValueError retry ONCE with the
    #    error message appended to the user message; second failure → fallback schema
    #    {"version":1,"entity":None → null,"pipeline":{"stages":["in_progress"],"terminal":["done"]}}.
    # 2. Candidate examples: union of search_repo.search_threads(q=probe, limit=20)
    #    over keyword_probes (cap 60 unique threads) — the FTS prefilter; fall back to
    #    _read_candidates(limit=40) when probes hit nothing.
    # 3. Score candidates with the EXISTING tasks._score_all(db, user_id=..., candidates=...,
    #    name=proposal name, description=description) → positives (≥7) / near_misses (4-6), top 5 each.
    # 4. draft_cache.store_result BEFORE _publish(user_id, "task_draft_ready",
    #    {"draft_id": ...}) — cache-before-publish, payload = {"proposal": {name, description,
    #    state_schema, keyword_probes}, "positives": [...], "near_misses": [...]}.
```

(`_score_all`'s candidate dict shape is `{"thread_id", "gmail_thread_id", "subject", "sender", "body_preview"}` — adapt search hits to it.)

**Steps:**
- [ ] Failing tests: parse_response shape checks; worker happy path (canned LLM JSON → cache ready with validated schema, publish after cache — assert ordering via a recording fake); invalid-schema-then-valid retry path; double-invalid → fallback schema; probes-miss → `_read_candidates` fallback.
- [ ] Implement → pass. Commit: `feat(task-engine): goal → proposed schema/criteria draft flow`

---

### Task 9: Backfill

**Files:** Add `backfill_task` to `server/app/workers/task_engine_tasks.py`; Test extend `server/tests/test_task_engine_tasks.py`.

```python
@celery_app.task(name="app.workers.task_engine_tasks.backfill_task")
def backfill_task(user_id: str, task_id: str, keyword_probes: list[str] | None = None) -> None
    # Candidates: FTS union over probes (cap 500) ∪ newest 200 threads (list_threads
    # include_archived=True — archived threads are still task history).
    # Relevance: triage() per candidate with ONLY this tracker in the tracker list and
    # buckets=[] (bucket pick ignored; write NO bucket_id) → link when conf ≥ task_link_confidence.
    # Extraction: over matched threads in ASCENDING last_activity_at order (pipelines
    # replay history correctly) via engine.extract_for_pair, committing per thread.
    # Progress: _publish(user_id, "task_backfill_progress",
    # {"task_id", "scanned", "matched", "done": false}) every 50 scanned; terminal
    # publish with done=true + a final task_updated. Idempotent by construction
    # (link upsert + event unique index) — safe to re-run.
```

**Steps:**
- [ ] Failing tests (canned triage + extraction responses): matched threads linked + extracted ascending (assert event order by seeding two matches with different activity dates and canned stage transitions — final state reflects the LATER message); progress publishes captured incl. terminal `done: true`; re-run → no dupes; user-detached link never re-attached during backfill.
- [ ] Implement → pass. Commit: `feat(task-engine): FTS-prefiltered chronological backfill`

---

### Task 10: API router — draft, CRUD, board, corrections

**Files:** Create `server/app/api/tasks.py`; Modify `server/app/main.py`; Test `server/tests/test_tasks_api.py` (authed-client pattern from `test_inbox_api.py`).

**Routes (all `Depends(get_current_user)`; ownership via `repo.get_owned_task` → 404; serializers return plain dicts):**

```
POST   /api/tasks/draft {goal}                    → 202 {draft_id}; draft_cache.mark_pending then enqueue propose_task_draft
GET    /api/tasks/draft/{draft_id}                → 200 ready payload | 202 {"status":"pending"} | 404 | 403 (mirror buckets draft endpoint)
POST   /api/tasks {name, goal, description, state_schema, keyword_probes?,
                   confirmed_positives?, confirmed_negatives?}     → 201 task
       # validate_schema (422 w/ message on ValueError); criteria via formulate_criteria;
       # commit; enqueue backfill_task(user.id, task.id, keyword_probes)
GET    /api/tasks                                 → {"tasks":[{id,name,goal,kind,status,version,
                                                     "summary":{"entities": n, "pending_reviews": n, "last_event_at": ts|null}}]}
GET    /api/tasks/{id}                            → task detail incl. state_schema + summary
PATCH  /api/tasks/{id} {name? | status? | state_schema?}
       # status ∈ {active,paused}; state_schema → validate_schema + assert_additive_change(409 on violation); bump version
DELETE /api/tasks/{id}                            → 204 soft delete (idempotent like buckets)

GET    /api/tasks/{id}/board                      → {"entities":[{id, entity_key, display_name, state, updated_at}]}
GET    /api/tasks/{id}/events?status=&entity_id=&page=&limit=      → newest-first event feed (serialize all provenance fields)
GET    /api/tasks/{id}/threads                    → attached threads serialized via api.inbox._serialize_thread
POST   /api/tasks/{id}/threads {thread_id}        → user attach: upsert_link(origin='user'); 404 unless the
                                                    thread is the user's; enqueue extract_for_thread; 201
DELETE /api/tasks/{id}/threads/{thread_id}        → user detach: link (origin='user', state='detached');
                                                    all APPLIED events sourced from that thread → status='reverted';
                                                    refold touched entities; 204
POST   /api/tasks/{id}/events/{event_id}/approve  → pending_review → apply_event; 409 if not pending
POST   /api/tasks/{id}/events/{event_id}/reject   → pending_review → 'rejected'; 409 if not pending
POST   /api/tasks/{id}/events/{event_id}/revert   → applied → 'reverted' + refold_entity; 409 if not applied
POST   /api/tasks/{id}/entities/{entity_id}/state {field, value}
       → user edit: coerce via schema; append_event(origin='user') + apply_event (this event IS the fence)
POST   /api/tasks/{id}/entities/{entity_id}/merge {into_entity_id}
       → repoint the loser's events' entity_id to the winner, refold winner, delete loser row; 204
```

Every mutating route: `db.commit()` then `tasks._publish(user.id, "task_updated", {"task_id", "version", "pending_count"})` (import the helper; publish-after-commit). All correction writes are synchronous (no Celery except the two enqueues noted).

**Steps:**
- [ ] Failing tests (LLM never called — monkeypatch celery enqueues where needed): draft 202 + pending poll + ready poll + cross-user 403; create validates schema (422 bad, 201 good, backfill enqueued); list/detail summaries; PATCH additive ok / destructive 409 / pause; soft-delete idempotent; attach (other user's thread 404, extraction enqueued) / detach reverts + refolds (seed applied events from that thread, assert entity state recomputed); approve/apply, reject, revert+refold with 409 wrong-state guards; manual state edit coerces + fences (subsequent older-message LLM proposal would pend — assert the user event exists as the fence anchor); merge repoints + refolds; every mutation publishes `task_updated` with bumped version (capture `_publish`).
- [ ] Implement → pass. Full suite green. Commit: `feat(api): task draft/CRUD/board/correction endpoints`

---

### Task 11: Reference docs

**Files:** Create `reference/TASKS_INDEX.md`; Modify `reference/WORKERS_INDEX.md`, `reference/INBOX_SYNC_INDEX.md`, `reference/MANIFEST.md` (+ row for the new doc).

- [ ] `TASKS_INDEX.md`: dense index of `task_engine/` (files/exports table, the 8 validator steps, sticky-link + fence + refold gotchas, confidence settings, SSE event vocabulary `task_draft_ready`/`task_updated`/`task_backfill_progress`, the celery task table, API route table). Verify every claim against source.
- [ ] `WORKERS_INDEX.md`: triage replaces classify in `_triage_batch` (dual-write); new celery module + tasks; enqueue hooks. `INBOX_SYNC_INDEX.md`: sync flow gains the process_task_updates enqueue edge. `MANIFEST.md`: new row + re-stamps.
- [ ] Stamp all touched docs to the final code commit per the repo rule. Commit: `docs(reference): TASKS_INDEX + re-index workers/sync for phase 2a`
- [ ] Final: `uv run pytest -q 2>&1 | tail -3` green; `uv run alembic upgrade head` against dev-stack Postgres applies 0007 cleanly.
