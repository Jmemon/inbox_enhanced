<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 2 — data-layer first -->

# Storage & Search — Postgres FTS for the HUD EDA loop

> Goal: **G4 (self-sufficient storage — no Gmail round-trips in steady state)**
> and **G5 (sub-100ms text search at inbox scale)**, with migrations that
> evolve `server/app/db/models.py` rather than replace it.

## 1. Decision: Postgres-native search

**Committed: full message text in `inbox_messages` + a generated `tsvector`
column with a GIN index for FTS + `pg_trgm` GIN indexes for
substring/fuzzy matching on sender and subject.** No external search engine.

Justification within the stack:

- Scale: a personal inbox is 10⁴–10⁵ messages. GIN-indexed
  `websearch_to_tsquery` over that is single-digit milliseconds; G5's 100ms
  budget has two orders of magnitude headroom.
- Ops: Elasticsearch/Typesense on Railway = a fourth service, a sync pipeline
  (the exact class of drift bug this plan exists to kill), and a second query
  language. Rejected.
- Transactionality: the tsvector is a **generated column** — search is updated
  in the same transaction as the upsert. The search index can never lag the
  data; that property is free in Postgres and expensive everywhere else.
- pg_trgm covers what FTS is bad at: partial sender matching
  (`from:goog` → `google.com` addresses) and typo-tolerant subject lookup —
  both core EDA gestures.

## 2. Schema changes (model deltas + migration sketches)

### 2.1 Migration `0006_bodies_and_sync_state.py` (Phase 0 + Phase 2 columns)

Against current `server/app/db/models.py`:

```python
# --- inbox_messages ---
op.add_column("inbox_messages", sa.Column("body_text", sa.Text()))           # full decoded body (plain pref, html fallback), capped 1MB at ingest
op.add_column("inbox_messages", sa.Column("labels", sa.dialects.postgresql.JSONB(),
                                          server_default="[]"))              # gmail labelIds snapshot
op.add_column("inbox_messages", sa.Column("is_unread", sa.Boolean(),
                                          nullable=False, server_default="false"))
op.add_column("inbox_messages", sa.Column("is_deleted", sa.Boolean(),
                                          nullable=False, server_default="false"))

# --- inbox_threads ---
op.add_column("inbox_threads", sa.Column("last_activity_at", sa.BigInteger()))  # denormalized recent gmail_internal_date
op.add_column("inbox_threads", sa.Column("is_archived", sa.Boolean(),
                                         nullable=False, server_default="false"))
op.add_column("inbox_threads", sa.Column("processing_state", sa.String(16),
                                         nullable=False, server_default="clean"))  # 'clean'|'enriching'|'failed'  (see agent-2-03)
op.create_index("ix_inbox_threads_user_activity", "inbox_threads",
                ["user_id", sa.text("last_activity_at DESC NULLS LAST")])

# --- users (sync state, see agent-2-01) ---
op.add_column("users", sa.Column("gmail_watch_expiration", sa.BigInteger()))
op.add_column("users", sa.Column("gmail_sync_status", sa.String(16),
                                 nullable=False, server_default="ok"))
```

Model edits: `InboxMessage.body_text/labels/is_unread/is_deleted`,
`InboxThread.last_activity_at/is_archived/processing_state`,
`User.gmail_watch_expiration/gmail_sync_status`.

`last_activity_at` is maintained in `inbox_repo.upsert_message` next to the
existing `recent_message_id` recompute — it kills the
`outerjoin(InboxMessage, recent_message_id)` sort in
`inbox_repo.list_threads` and `tasks._read_candidates` (both become a plain
indexed `ORDER BY last_activity_at DESC`).

Ingest changes (`server/app/gmail/parser.py` → `inbox_repo.upsert_message`):
`ParsedMessage.body_text` (already exists in the dataclass, currently
unpersisted) flows through `gmail_sync._upsert_thread_with_messages` into the
new column. `body_preview` stays (cheap list rendering).

**Backfill (OQ 3.1):** forward-fill on every sync touch + a one-shot
backfill task `backfill_bodies(user_id)` (enqueued per user on next SSE
connect, lock-held, batched 50 threads at a time through the new
thread-pooled fetcher). Rollback path: columns are additive/nullable; the old
code path simply ignores them.

### 2.2 Migration `0007_search.py` (Phase 1)

```python
op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
op.execute("""
    ALTER TABLE inbox_messages ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(subject_cache, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(from_addr, '')),     'B') ||
        setweight(to_tsvector('english', left(coalesce(body_text, ''), 200000)), 'C')
    ) STORED
""")
op.execute("CREATE INDEX ix_inbox_messages_tsv ON inbox_messages USING GIN (search_tsv)")
op.execute("CREATE INDEX ix_inbox_messages_from_trgm ON inbox_messages USING GIN (from_addr gin_trgm_ops)")
op.execute("CREATE INDEX ix_inbox_threads_subject_trgm ON inbox_threads USING GIN (subject gin_trgm_ops)")
```

Notes:

- `subject_cache`: messages don't store subject today (it lives on the
  thread); add `inbox_messages.subject_cache Text` in 0006 (copied from the
  parsed message — `parse_message` already extracts it) so the tsvector is
  fully message-local. Generated columns can't reference other tables.
- `'english'` config, pinned. Mixed-language inboxes degrade to
  stemming-less matches, and the trgm indexes backstop them. Revisit with
  per-user config only if real demand appears (YAGNI).
- `left(..., 200000)`: tsvector inputs have hard limits; cap body
  contribution defensively.

## 3. Query layer

New module **`server/app/inbox/search_repo.py`**:

```python
def search_threads(db, *, user_id, q, bucket_id=None, task_id=None,
                   from_contains=None, after_ms=None, before_ms=None,
                   include_archived=False, limit=50, offset=0) -> list[ThreadHit]
```

- Primary predicate: `inbox_messages.search_tsv @@ websearch_to_tsquery('english', q)`
  (websearch syntax gives users quotes/`-`/`OR` for free), grouped to threads
  (`DISTINCT ON (thread_id)` ordered by `ts_rank_cd` then
  `last_activity_at DESC`).
- If FTS yields < 5 hits, union a trgm fallback:
  `from_addr % :q OR subject ILIKE '%'||:q||'%'` (catches partial addresses
  and stems FTS misses).
- Filters compose as plain WHERE clauses; `task_id` joins `task_links`
  (Phase 4).
- Returns the same thread shape `api/inbox._serialize_thread` emits, plus
  `match_snippet` from `ts_headline` on the best-ranked message.

Route: **`GET /api/search`** in a new `server/app/api/search.py`, wired in
`app/main.py` next to the existing routers. Contract in `agent-2-05`.

## 4. LLM paths stop touching Gmail (Phase 0 payoff)

With `body_text` persisted, three hot paths in `server/app/workers/tasks.py`
lose their Gmail refetch loops:

- `_reclassify_all` — currently refetches **every** thread
  (`gmail.users().threads().get` sequentially, the reason for the 60s/150s
  client watchdogs in `Home.tsx`). Becomes: load threads + messages from
  Postgres, rebuild `ParsedThread`s via a new
  `parser.thread_from_rows(thread, messages)` helper, classify. A 200-thread
  reclassify drops from ~110s to LLM-bound (~10–15s under
  `LLM_CONCURRENCY=16`).
- `_score_all` (draft preview) — same substitution; the "~200ms each,
  unavoidable" comment in `tasks.py` becomes false and the code honest.
- Task extraction (Phase 4) is born Postgres-only.

`thread_to_string` is unchanged — it already consumes `ParsedThread`.

## 5. Attachments (metadata only)

Migration 0006 adds:

```python
op.create_table("inbox_attachments",
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("message_id", sa.String(36), sa.ForeignKey("inbox_messages.id"), index=True, nullable=False),
    sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), index=True, nullable=False),
    sa.Column("gmail_attachment_id", sa.Text(), nullable=False),
    sa.Column("filename", sa.Text()), sa.Column("mime_type", sa.String(255)),
    sa.Column("size_bytes", sa.Integer()),
)
```

`parser._find_body_by_mime` already walks parts and skips
`body.attachmentId` entries; the walk now also records them. Bytes are never
stored — `GET /api/messages/{id}/attachments/{aid}` proxies Gmail lazily
(`agent-2-01` §6). Filenames are worth indexing later if EDA demands it
(deferred, noted).
