<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 1 — incremental evolution -->

# Data Model & Storage Plan

All models live in `server/app/db/models.py` alongside the existing `User`,
`UserSession`, `Bucket`, `InboxThread`, `InboxMessage`. Conventions copied from
what exists: `String(36)` uuid-hex PKs, per-user `UniqueConstraint` race guards
(mirroring `uq_inbox_threads_user_gmail`), soft pointers where FKs would be
chicken/egg, JSONB for schema-bearing blobs. Migrations follow the style of
`server/migrations/versions/0003_buckets_v2.py` (data migrations inline,
deterministic ids where round-tripping matters).

---

## Phase 0 — Migration `0006_message_bodies_fts`

### Changes to `InboxMessage`

```python
class InboxMessage(Base):
    ...existing columns...
    # Full plain-text body (parser already extracts it: gmail/parser.py
    # ParsedMessage.body_text feeds thread_to_string; today it's thrown away
    # after the 200-char body_preview is cut). Nullable: pre-migration rows
    # have no body until next sync touches them (forward-fill).
    body_text: Mapped[str | None] = mapped_column(Text)
```

Writer change: `gmail_sync._upsert_thread_with_messages` passes the parsed
`body_text` through to `inbox_repo.upsert_message` (new kwarg). No backfill
task initially — full sync / extend / partial sync forward-fill naturally, and
`full_sync_inbox` wipes+repopulates anyway. An optional `backfill_bodies(user_id)`
Celery task can be added if Phase 3 testing shows too many body-less rows.

### FTS

Migration adds (raw SQL via `op.execute`, since SQLAlchemy's computed-column
support for tsvector is clumsy):

```sql
ALTER TABLE inbox_messages ADD COLUMN search_tsv tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(from_addr,'')), 'A') ||
    setweight(to_tsvector('english', coalesce(body_text,'')),  'B')
  ) STORED;
CREATE INDEX ix_inbox_messages_search_tsv ON inbox_messages USING GIN (search_tsv);
-- subjects live on the thread:
ALTER TABLE inbox_threads ADD COLUMN subject_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', coalesce(subject,''))) STORED;
CREATE INDEX ix_inbox_threads_subject_tsv ON inbox_threads USING GIN (subject_tsv);
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_inbox_messages_from_trgm ON inbox_messages USING GIN (from_addr gin_trgm_ops);
```

Query layer: new `server/app/inbox/search_repo.py` with
`search_threads(db, *, user_id, q, task_id=None, limit, offset)` using
`websearch_to_tsquery('english', :q)` against
`inbox_messages.search_tsv || inbox_threads.subject_tsv` (join on
`thread_id`), ranked by `ts_rank` + recency tiebreak on
`gmail_internal_date`. pg_trgm covers fuzzy sender lookup ("from:goog").
Served by `GET /api/search` (see `agent-1-sync-api-frontend.md`).

### Downstream wins (same phase)

- `tasks._reclassify_all` and `tasks._score_all` stop calling
  `gmail.users().threads().get(format="full")` per thread; they reconstruct the
  classify input from Postgres (`subject` + ordered `body_text` per message —
  a small `inbox_repo.load_thread_text(db, thread_id)` helper replicating
  `gmail/parser.thread_to_string` output shape). Reclassify of a 200-thread
  inbox drops from ~40s of Gmail round-trips to one DB query.
- Blob storage for attachments/HTML (open question 1.2 in
  `specs/002_inbox_sync/05_18_26-open-questions.md`) stays **out of scope**:
  task extraction needs text, not MIME parts. YAGNI until a task type demands
  attachments.

---

## Phase 2 — Migration `0007_push_and_label_sync`

```python
class User(Base):
    ...existing...
    # Gmail push (users.watch) bookkeeping. Watch expires ~7 days; a beat job
    # renews daily. Null = push not established (poll-only fallback).
    gmail_watch_expiration: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_watch_topic: Mapped[str | None] = mapped_column(String(255))

class InboxThread(Base):
    ...existing...
    # Mirrors Gmail INBOX-label removal (archive). Archived threads stay
    # queryable (tasks may reference them) but default inbox views filter them.
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                              server_default="false")
```

`messageDeleted` history records hard-delete the `inbox_messages` row and
recompute `recent_message_id` via the existing
`inbox_repo.upsert_message` recompute query; a thread whose last message is
deleted is deleted too. No tombstones — Gmail is the source of truth.

---

## Phase 3 — Migration `0008_tasks_core`

The heart of the plan. Four tables.

### `tasks`

```python
class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # null user_id => default classify-task shared by all users (inherited
    # from the Bucket convention so the Phase 4 unification is a no-op here).
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The user's natural-language goal, verbatim ("find a job"). Empty for
    # migrated buckets.
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Relevance criteria — IDENTICAL grammar to Bucket.criteria today:
    # description + "Example cases:" + <positive>/<nearmiss> blocks
    # (bucket_repo.formulate_criteria's output). The classify prompt keeps working.
    criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 'classify' (degenerate / ex-bucket: relevance only, exclusive single-
    # assignment, drives inbox pills) | 'track' (relevance + state engine).
    # Action capability is NOT a kind — it's the action_mode dial below,
    # because per VISION.md acting is a capability of tasks, not a species.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="track")
    # State schema, constrained vocabulary (see agent-1-task-engine.md §1).
    # null for kind='classify'.
    state_schema: Mapped[dict | None] = mapped_column(JSONB)
    # 'off' | 'propose' | 'auto' — Phase 5; column exists from day one so no
    # second tasks-table migration is needed.
    action_mode: Mapped[str] = mapped_column(String(16), nullable=False,
                                             default="off", server_default="'off'")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active|paused
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                             server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

### `task_thread_links` — relevance, with manual-correction semantics

```python
class TaskThreadLink(Base):
    __tablename__ = "task_thread_links"
    __table_args__ = (
        # One verdict per (task, thread); upserts race-safe like inbox tables.
        UniqueConstraint("task_id", "thread_id", name="uq_task_thread"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("inbox_threads.id"),
                                           nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    # 'llm' | 'user'. User-origin rows are pins/blocks: the relevance
    # classifier NEVER updates a row whose origin is 'user'.
    origin: Mapped[str] = mapped_column(String(8), nullable=False)
    # 'attached' | 'detached'. (origin='user', state='detached') = "never
    # attach this thread to this task again".
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="attached")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Many-to-many by design: a thread can feed several tasks. This coexists with the
exclusive `inbox_threads.bucket_id` until Phase 4 — classify-kind tasks keep
using `bucket_id` (renamed in spirit to "primary classification") because the
inbox list needs exactly one pill per row.

### `task_state_entities` — current state (the board)

```python
class TaskStateEntity(Base):
    __tablename__ = "task_state_entities"
    __table_args__ = (
        UniqueConstraint("task_id", "entity_key", name="uq_task_entity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    # Normalized grouping key per state_schema.entity_by (e.g. "anthropic").
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Current field values, {field_name: value}. ALWAYS derivable as a fold
    # over applied task_events — this column is a materialization, and
    # task_engine.refold_entity() rebuilds it after revert/reject.
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Singleton tasks (no `entity_by`) get one row with `entity_key="_self"`.

### `task_events` — append-only transition log (audit + correction substrate)

```python
class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        # Idempotency: re-syncing the same message can't double-apply the same
        # field transition. message_id/field nullable for user-origin edits →
        # partial unique index in the migration:
        #   CREATE UNIQUE INDEX uq_task_event_msg_field ON task_events
        #     (task_id, message_id, field) WHERE message_id IS NOT NULL;
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("task_state_entities.id"), index=True)
    # Provenance: which email produced this transition. Soft pointers (same
    # rationale as InboxThread.recent_message_id) — a hard-deleted message
    # must not cascade away the audit trail.
    thread_id: Mapped[str | None] = mapped_column(String(36))
    message_id: Mapped[str | None] = mapped_column(String(36))
    field: Mapped[str | None] = mapped_column(String(64))      # schema field name
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    # Verbatim quote from the email that justifies the transition (rendered in
    # the UI next to the change; the manual-correction loop lives on this).
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(BigInteger)  # 0–100
    origin: Mapped[str] = mapped_column(String(8), nullable=False)  # 'llm' | 'user'
    # 'applied' | 'pending_review' | 'rejected' | 'reverted'
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
```

Repo layer: new `server/app/tasks_engine/task_repo.py` mirroring
`inbox_repo.py`'s contract — **never commits, caller owns the txn** — with
`upsert_link`, `get_or_create_entity`, `append_event`, `apply_event`,
`refold_entity` (replay applied events oldest→newest, user-origin events win
ties), `list_board`, `list_events`.

---

## Phase 4 — Migration `0009_buckets_become_tasks`

ID-preserving unification (the trick that makes this phase small):

1. `INSERT INTO tasks (id, user_id, name, goal, criteria, kind, status, is_deleted, created_at)
   SELECT id, user_id, name, '', criteria, 'classify', 'active', is_deleted, now() FROM buckets;`
   — **same primary keys**, so nothing referencing a bucket id breaks.
2. Drop FK `inbox_threads.bucket_id → buckets.id`; re-create it pointing at
   `tasks.id`. Zero row updates.
3. Drop table `buckets` (downgrade re-creates and reverse-copies `kind='classify'` rows).
4. Code: `bucket_repo.list_active` → `task_repo.list_classify_tasks`;
   `api/buckets.py` routes delegate to task equivalents for one release
   (response shape unchanged: `_serialize` reads the task row), then are removed
   together with `client/src/pages/buckets/useBuckets.tsx`'s API constants
   switching to `/api/tasks?kind=classify`.
5. `llm/default_criteria.py::INITIAL_DEFAULT_BUCKETS` seeds default
   classify-tasks for new users — unchanged content, new table.

## Phase 5 — Migration `0010_task_actions`

```python
class TaskAction(Base):
    __tablename__ = "task_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    # 'archive_thread' | 'label_thread' | 'draft_reply'  (closed vocabulary)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Action arguments, e.g. {"thread_id": ..., "label": ...} or
    # {"thread_id": ..., "draft_body": ...}
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Provenance: the task_event that motivated the action.
    source_event_id: Mapped[str | None] = mapped_column(String(36))
    # 'proposed' | 'approved' | 'executed' | 'rejected' | 'failed'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskActionGrant(Base):
    """Per-task, per-action-type consent record. 'auto' execution requires a
    matching grant row; 'propose' requires none (each action is approved
    individually)."""
    __tablename__ = "task_action_grants"
    __table_args__ = (UniqueConstraint("task_id", "action_type", name="uq_task_action_grant"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Plus `users.gmail_granted_scopes: Mapped[str | None]` (space-joined scope list,
updated at OAuth callback) so the API can tell whether action execution is even
possible before enqueueing.
