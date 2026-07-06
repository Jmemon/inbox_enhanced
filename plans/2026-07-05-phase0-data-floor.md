# Phase 0 — Data Floor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Spec: `specs/004_vision_arch/chosen-architecture.md` §3 Phase 0 (stamped `cd1ea86`).
> Plan stamped at commit `ed74bc2` on branch `main`.

**Goal:** Give the task engine its data floor: full message bodies in Postgres, a full sync that reconciles instead of wiping, Gmail archive/delete/unread mirroring, Postgres FTS + `GET /api/search` (with a search box in the inbox UI), reclassify/preview reading Postgres instead of re-fetching Gmail, and a persisted `llm_calls` metrics table.

**Architecture:** All changes evolve the existing FastAPI + Celery + Postgres + Redis stack — one Alembic migration (`0006_data_floor`), new columns on `inbox_messages`/`inbox_threads`, one new table (`llm_calls`), one new repo module (`search_repo`), one new router (`api/search.py`), one new LLM metrics module. No new services, no new env vars.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 / Alembic / Celery / Postgres (FTS: tsvector + GIN + pg_trgm) / React 19 + Vite + TypeScript (bun).

## Global Constraints

- Python deps via `uv` only; JS via `bun` only. Never hand-edit `server/pyproject.toml`.
- NEVER read `.env`. Only `.env.example` (no new env vars in this phase).
- All server commands run from `server/`: `cd server && uv run pytest …`.
- Pipe test output: `uv run pytest -q 2>&1 | tail -20` (full-suite checks: `| tail -5`).
- **Tests run on SQLite in-memory** (`tests/conftest.py`), and migration tests upgrade a SQLite file DB to `head`. Therefore ALL Postgres-only DDL (tsvector generated columns, GIN indexes, `pg_trgm`) MUST be guarded with `if op.get_bind().dialect.name == "postgresql":` and ALL Postgres-only query paths need a non-PG fallback branch.
- Repo functions (`inbox_repo`, `search_repo`) NEVER commit — the caller owns the transaction.
- Workers publish AFTER commit (existing `_publish` contract) — do not reorder.
- Beat stays single-replica; this phase adds no beat entries.
- The frontend bundle in `server/app/static/` is generated — never edit; client verification is `cd client && bun run build`.
- Commit after every task, message style: `type(scope): summary` (no attribution lines).

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/app/db/models.py` | modify | +`InboxMessage.{body_text,labels,is_unread,is_deleted}`, +`InboxThread.{is_archived,last_activity_at}`, +`LlmCall` |
| `server/migrations/versions/0006_data_floor.py` | create | all Phase-0 schema (dialect-guarded FTS DDL) + `last_activity_at` backfill |
| `server/app/gmail/parser.py` | modify | `ParsedMessage.label_ids` (from Gmail `labelIds`) |
| `server/app/inbox/inbox_repo.py` | modify | persist body/labels/unread; `recompute_thread_pointers`; archived filter + `last_activity_at` sort; `load_parsed_threads` |
| `server/app/workers/gmail_sync.py` | modify | pass body/labels through; reconcile full sync; widened `historyTypes` + delete/label record handling |
| `server/app/workers/tasks.py` | modify | `_reclassify_all` / `_score_all` read Postgres |
| `server/app/inbox/search_repo.py` | create | dialect-aware thread search (PG FTS / fallback ILIKE) |
| `server/app/api/search.py` | create | `GET /api/search` |
| `server/app/api/inbox.py` | modify | `include_archived` param; serialize `is_archived`/`is_unread` |
| `server/app/main.py` | modify | register search router |
| `server/app/llm/metrics.py` | create | `record_call` → `llm_calls` row (never raises) |
| `server/app/llm/client.py` | modify | instrument `call_messages` (usage, cost, duration, outcome) |
| `server/app/llm/classify.py` | modify | pass `stage`/`user_id` through |
| `client/src/lib/api.ts` | modify | `is_archived` on `InboxThread`; `searchInbox()` |
| `client/src/pages/inbox/useInbox.tsx` | modify | evict archived threads in `applyThreadUpdates` |
| `client/src/pages/Home.tsx` | modify | search box + results view |
| `reference/INBOX_SYNC_INDEX.md`, `reference/WORKERS_INDEX.md`, `reference/MANIFEST.md` | modify | re-index + re-stamp |

---

### Task 1: Migration 0006 + ORM columns (bodies, flags, `last_activity_at`, `llm_calls`, PG-only FTS)

**Files:**
- Modify: `server/app/db/models.py`
- Create: `server/migrations/versions/0006_data_floor.py`
- Test: `server/tests/test_migration_0006.py`

**Interfaces:**
- Consumes: existing `Base`, `InboxThread`, `InboxMessage` models; migration `0005_newsletter_v2` as `down_revision`.
- Produces: columns `inbox_messages.body_text/labels/is_unread/is_deleted`, `inbox_threads.is_archived/last_activity_at`, table `llm_calls` (model `LlmCall`). Later tasks rely on these exact names.

- [ ] **Step 1: Write the failing migration test**

Create `server/tests/test_migration_0006.py` (fixture pattern copied from `test_migration_0005.py`):

```python
"""Integration test for migration 0006 (data floor).

Upgrades a fresh SQLite db to head and asserts the new columns/table exist.
FTS objects (tsvector generated columns, GIN, pg_trgm) are Postgres-only and
dialect-guarded in the migration — they are intentionally NOT asserted here.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_cfg(db_url: str) -> Config:
    os.environ["DATABASE_URL"] = db_url
    from app.config import get_settings
    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


@pytest.fixture
def migrated_db(tmp_path):
    db_path = tmp_path / "mig.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    cfg = _alembic_cfg(db_url)
    command.upgrade(cfg, "head")
    eng = create_engine(db_url, future=True)
    yield eng, cfg
    eng.dispose()


def _cols(eng, table: str) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(table)}


def test_new_columns_exist_after_upgrade(migrated_db):
    eng, _ = migrated_db
    assert {"body_text", "labels", "is_unread", "is_deleted"} <= _cols(eng, "inbox_messages")
    assert {"is_archived", "last_activity_at"} <= _cols(eng, "inbox_threads")


def test_llm_calls_table_exists(migrated_db):
    eng, _ = migrated_db
    assert {"id", "user_id", "task_id", "stage", "model", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "cost_usd", "ttft_ms", "duration_ms", "outcome",
            "created_at"} <= _cols(eng, "llm_calls")


def test_downgrade_removes_everything(migrated_db):
    eng, cfg = migrated_db
    command.downgrade(cfg, "0005_newsletter_v2")
    assert "body_text" not in _cols(eng, "inbox_messages")
    assert "is_archived" not in _cols(eng, "inbox_threads")
    assert not inspect(eng).has_table("llm_calls")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_migration_0006.py -q 2>&1 | tail -5`
Expected: FAIL (ERROR at upgrade — revision `0006_data_floor` not found, or column assertions fail).

- [ ] **Step 3: Add ORM columns + `LlmCall` model**

In `server/app/db/models.py`, update imports:

```python
from sqlalchemy import (Boolean, String, Text, DateTime, ForeignKey, BigInteger,
                        Integer, Float, JSON, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
```

Append to `InboxThread` (after `recent_message_id`):

```python
    # Mirrors Gmail INBOX-label removal (archive). Archived threads stay
    # queryable (task evidence) but default inbox views filter them.
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                              server_default="false")
    # Denormalized max(gmail_internal_date) of non-deleted messages. Maintained
    # by inbox_repo.recompute_thread_pointers; kills the recent-message
    # outerjoin sort in list_threads.
    last_activity_at: Mapped[int | None] = mapped_column(BigInteger)
```

Append to `InboxMessage` (after `body_preview`):

```python
    # Full decoded plain-text body (parser already extracts it; was discarded
    # after the preview cut). Nullable: pre-migration rows forward-fill on the
    # next sync touch.
    body_text: Mapped[str | None] = mapped_column(Text)
    # Gmail labelIds snapshot (stored, not interpreted beyond INBOX/UNREAD).
    labels: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"),
                                         nullable=False, default=list, server_default="[]")
    is_unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                            server_default="false")
    # Soft delete — task evidence must survive Gmail deletions.
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                             server_default="false")
```

Add at the end of the file:

```python
class LlmCall(Base):
    """One row per LLM API call (VISION: metrics persisted, not just logged).
    Written by app/llm/metrics.record_call from the llm client choke point."""
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36))  # tasks land in Phase 2
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # classify|score|extract|propose
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)  # null for non-streamed calls
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # success|error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
```

- [ ] **Step 4: Write migration `0006_data_floor.py`**

Create `server/migrations/versions/0006_data_floor.py`:

```python
"""data floor: bodies, mirror flags, last_activity_at, llm_calls, FTS (pg only)

Revision ID: 0006_data_floor
Revises: 0005_newsletter_v2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006_data_floor"
down_revision: Union[str, Sequence[str], None] = "0005_newsletter_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- inbox_messages ---
    op.add_column("inbox_messages", sa.Column("body_text", sa.Text(), nullable=True))
    op.add_column("inbox_messages", sa.Column(
        "labels", sa.JSON().with_variant(JSONB(), "postgresql"),
        nullable=False, server_default="[]"))
    op.add_column("inbox_messages", sa.Column(
        "is_unread", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("inbox_messages", sa.Column(
        "is_deleted", sa.Boolean(), nullable=False, server_default="false"))

    # --- inbox_threads ---
    op.add_column("inbox_threads", sa.Column(
        "is_archived", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("inbox_threads", sa.Column("last_activity_at", sa.BigInteger(), nullable=True))
    op.create_index("ix_inbox_threads_user_activity", "inbox_threads",
                    ["user_id", "last_activity_at"])
    # Backfill so pre-migration rows keep their sort position (list_threads
    # switches to ORDER BY last_activity_at).
    op.execute("""
        UPDATE inbox_threads SET last_activity_at = (
            SELECT MAX(inbox_messages.gmail_internal_date) FROM inbox_messages
            WHERE inbox_messages.thread_id = inbox_threads.id)
    """)

    # --- llm_calls ---
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_calls_user_id", "llm_calls", ["user_id"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])

    # --- FTS: Postgres only. SQLite (tests) skips this block; the search repo
    # has a non-PG fallback path so behavior stays testable. ---
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("""
            ALTER TABLE inbox_messages ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                setweight(to_tsvector('english', coalesce(from_addr, '')), 'A') ||
                setweight(to_tsvector('english', left(coalesce(body_text, ''), 200000)), 'B')
            ) STORED
        """)
        op.execute("CREATE INDEX ix_inbox_messages_search_tsv ON inbox_messages USING GIN (search_tsv)")
        op.execute("""
            ALTER TABLE inbox_threads ADD COLUMN subject_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(subject, ''))) STORED
        """)
        op.execute("CREATE INDEX ix_inbox_threads_subject_tsv ON inbox_threads USING GIN (subject_tsv)")
        op.execute("CREATE INDEX ix_inbox_messages_from_trgm ON inbox_messages USING GIN (from_addr gin_trgm_ops)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_inbox_messages_from_trgm")
        op.execute("DROP INDEX IF EXISTS ix_inbox_threads_subject_tsv")
        op.execute("DROP INDEX IF EXISTS ix_inbox_messages_search_tsv")
        op.execute("ALTER TABLE inbox_threads DROP COLUMN IF EXISTS subject_tsv")
        op.execute("ALTER TABLE inbox_messages DROP COLUMN IF EXISTS search_tsv")

    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_user_id", table_name="llm_calls")
    op.drop_table("llm_calls")

    op.drop_index("ix_inbox_threads_user_activity", table_name="inbox_threads")
    op.drop_column("inbox_threads", "last_activity_at")
    op.drop_column("inbox_threads", "is_archived")

    op.drop_column("inbox_messages", "is_deleted")
    op.drop_column("inbox_messages", "is_unread")
    op.drop_column("inbox_messages", "labels")
    op.drop_column("inbox_messages", "body_text")
```

- [ ] **Step 5: Run migration tests + full suite**

Run: `cd server && uv run pytest tests/test_migration_0006.py -q 2>&1 | tail -5`
Expected: 3 passed.
Run: `cd server && uv run pytest -q 2>&1 | tail -5`
Expected: all passing (new columns are additive; `conftest.py` uses `Base.metadata.create_all`, which picks up the model changes automatically).

- [ ] **Step 6: Commit**

```bash
git add server/app/db/models.py server/migrations/versions/0006_data_floor.py server/tests/test_migration_0006.py
git commit -m "feat(db): migration 0006 — bodies, mirror flags, last_activity_at, llm_calls, pg FTS"
```

---

### Task 2: Persist `body_text`/`labels`/`is_unread` + maintain `last_activity_at` through sync writes

**Files:**
- Modify: `server/app/gmail/parser.py` (ParsedMessage + parse_message)
- Modify: `server/app/inbox/inbox_repo.py` (upsert_message, new recompute_thread_pointers)
- Modify: `server/app/workers/gmail_sync.py:58-65` (`_upsert_thread_with_messages` pass-through)
- Test: `server/tests/test_inbox_repo.py`, `server/tests/test_message_parser.py`

**Interfaces:**
- Consumes: Task 1's columns.
- Produces: `ParsedMessage.label_ids: list[str]`; `inbox_repo.upsert_message(..., body_text: str | None = None, label_ids: list[str] | None = None)`; `inbox_repo.recompute_thread_pointers(db, *, thread: InboxThread) -> None` (recomputes `recent_message_id` + `last_activity_at`, excluding `is_deleted` messages). Tasks 4, 5, 7 rely on these.

- [ ] **Step 1: Write failing tests**

Append to `server/tests/test_message_parser.py`:

```python
def test_parse_message_captures_label_ids():
    raw = {
        "id": "m1", "threadId": "t1", "internalDate": "1000", "historyId": "5",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": [], "mimeType": "text/plain",
                    "body": {"data": "aGVsbG8="}},  # "hello"
    }
    from app.gmail.parser import parse_message
    m = parse_message(raw)
    assert m.label_ids == ["INBOX", "UNREAD"]
    assert m.body_text == "hello"
```

Append to `server/tests/test_inbox_repo.py` (reuse that file's existing user/thread seeding helpers — every test there creates a `User` row then calls `upsert_thread`; follow the same local pattern):

```python
def test_upsert_message_persists_body_labels_unread_and_activity(db):
    user = _mk_user(db)  # use the file's existing user-seeding helper/fixture
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id="gt1",
                             subject="s", bucket_id=None)
    msg = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt1", gmail_message_id="gm1",
        gmail_internal_date=111, gmail_history_id="7",
        to_addr="a@b.c", from_addr="x@y.z", body_preview="p",
        body_text="full body text", label_ids=["INBOX", "UNREAD"],
    )
    assert msg.body_text == "full body text"
    assert msg.labels == ["INBOX", "UNREAD"]
    assert msg.is_unread is True
    thread = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "gt1")).scalar_one()
    assert thread.last_activity_at == 111


def test_recompute_thread_pointers_skips_deleted_messages(db):
    user = _mk_user(db)
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id="gt2",
                             subject="s", bucket_id=None)
    old = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt2", gmail_message_id="gm-old",
        gmail_internal_date=100, gmail_history_id="1",
        to_addr=None, from_addr=None, body_preview=None)
    new = inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="gt2", gmail_message_id="gm-new",
        gmail_internal_date=200, gmail_history_id="2",
        to_addr=None, from_addr=None, body_preview=None)
    thread = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "gt2")).scalar_one()
    assert thread.recent_message_id == new.id

    new.is_deleted = True
    inbox_repo.recompute_thread_pointers(db, thread=thread)
    assert thread.recent_message_id == old.id
    assert thread.last_activity_at == 100

    old.is_deleted = True
    inbox_repo.recompute_thread_pointers(db, thread=thread)
    assert thread.recent_message_id is None
    assert thread.last_activity_at is None
```

(If `test_inbox_repo.py` has no `_mk_user` helper, add one matching how its existing tests create users — do not invent a new pattern.)

- [ ] **Step 2: Run to verify failure**

Run: `cd server && uv run pytest tests/test_message_parser.py tests/test_inbox_repo.py -q 2>&1 | tail -5`
Expected: FAIL (`label_ids` unexpected attribute / kwarg; `recompute_thread_pointers` missing).

- [ ] **Step 3: Implement parser change**

In `server/app/gmail/parser.py`: change the dataclass import to `from dataclasses import dataclass, field` and add to `ParsedMessage` (after `body_preview`):

```python
    # Gmail labelIds snapshot (INBOX/UNREAD interpreted at persist time).
    # default_factory so existing ParsedMessage(...) constructions stay valid.
    label_ids: list[str] = field(default_factory=list)
```

In `parse_message`, add to the constructor call:

```python
        label_ids=list(raw.get("labelIds", []) or []),
```

(Existing `ParsedMessage(...)` constructions in tests keep working via the default; verify with `rg "ParsedMessage\(" server/`.)

- [ ] **Step 4: Implement repo changes**

In `server/app/inbox/inbox_repo.py`, replace the recompute block at the end of `upsert_message` (lines 100–109) and extend the signature:

```python
def upsert_message(
    db: Session,
    *,
    user_id: str,
    gmail_thread_id: str,
    gmail_message_id: str,
    gmail_internal_date: int,
    gmail_history_id: str,
    to_addr: str | None,
    from_addr: str | None,
    body_preview: str | None,
    body_text: str | None = None,
    label_ids: list[str] | None = None,
) -> InboxMessage:
```

In both the insert and update branches, set the new fields (insert branch adds
constructor kwargs; update branch adds assignments):

```python
            body_text=body_text,
            labels=list(label_ids or []),
            is_unread="UNREAD" in (label_ids or []),
```

```python
        existing.body_text = body_text if body_text is not None else existing.body_text
        if label_ids is not None:
            existing.labels = list(label_ids)
            existing.is_unread = "UNREAD" in label_ids
```

Replace the trailing recompute with a call to the new helper:

```python
    recompute_thread_pointers(db, thread=thread)
    return existing
```

Add the helper after `upsert_message`:

```python
def recompute_thread_pointers(db: Session, *, thread: InboxThread) -> None:
    """Recompute recent_message_id + last_activity_at from the thread's
    non-deleted messages. Single indexed lookup (gmail_internal_date is
    indexed). Soft-deleted messages are invisible to both pointers so a
    Gmail deletion demotes the thread's sort position instead of pinning it."""
    row = db.execute(
        select(InboxMessage.id, InboxMessage.gmail_internal_date)
        .where(InboxMessage.thread_id == thread.id,
               InboxMessage.is_deleted == False)  # noqa: E712
        .order_by(InboxMessage.gmail_internal_date.desc())
        .limit(1)
    ).first()
    if row is None:
        thread.recent_message_id = None
        thread.last_activity_at = None
    else:
        thread.recent_message_id = row[0]
        thread.last_activity_at = row[1]
```

- [ ] **Step 5: Pass through in the sync writer**

In `server/app/workers/gmail_sync.py` `_upsert_thread_with_messages`, extend the `upsert_message` call:

```python
        inbox_repo.upsert_message(
            db, user_id=user_id, gmail_thread_id=parsed.gmail_thread_id,
            gmail_message_id=m.gmail_message_id,
            gmail_internal_date=m.gmail_internal_date,
            gmail_history_id=m.gmail_history_id,
            to_addr=m.to_addr, from_addr=m.from_addr, body_preview=m.body_preview,
            body_text=m.body_text, label_ids=m.label_ids,
        )
```

- [ ] **Step 6: Run tests**

Run: `cd server && uv run pytest tests/test_message_parser.py tests/test_inbox_repo.py tests/test_partial_sync.py -q 2>&1 | tail -5`
Expected: PASS.
Run full suite: `cd server && uv run pytest -q 2>&1 | tail -5` — expected all pass.

- [ ] **Step 7: Commit**

```bash
git add server/app/gmail/parser.py server/app/inbox/inbox_repo.py server/app/workers/gmail_sync.py server/tests/test_message_parser.py server/tests/test_inbox_repo.py
git commit -m "feat(sync): persist full bodies, labels, unread; maintain last_activity_at"
```

---

### Task 3: Archived filter + `last_activity_at` sort + API `include_archived` + serializer flags

**Files:**
- Modify: `server/app/inbox/inbox_repo.py:114-131` (`list_threads`)
- Modify: `server/app/api/inbox.py` (`list_inbox`, `_serialize_thread`, `_serialize_message`)
- Test: `server/tests/test_inbox_repo.py`, `server/tests/test_inbox_api.py`

**Interfaces:**
- Consumes: Task 1 columns, Task 2 `last_activity_at` maintenance.
- Produces: `inbox_repo.list_threads(db, *, user_id, limit, offset, include_archived: bool = False)`; `GET /api/inbox?include_archived=` ; thread JSON gains `"is_archived": bool`, message JSON gains `"is_unread": bool`. Task 9 (client) relies on `is_archived`.

- [ ] **Step 1: Write failing tests**

Append to `server/tests/test_inbox_repo.py`:

```python
def test_list_threads_excludes_archived_by_default_and_sorts_by_activity(db):
    user = _mk_user(db)
    for gid, date in (("g-old", 100), ("g-new", 300), ("g-arch", 200)):
        inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id=gid,
                                 subject=gid, bucket_id=None)
        inbox_repo.upsert_message(
            db, user_id=user.id, gmail_thread_id=gid, gmail_message_id=f"m-{gid}",
            gmail_internal_date=date, gmail_history_id="1",
            to_addr=None, from_addr=None, body_preview=None)
    arch = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-arch")).scalar_one()
    arch.is_archived = True

    listed = inbox_repo.list_threads(db, user_id=user.id, limit=10, offset=0)
    assert [t.gmail_id for t in listed] == ["g-new", "g-old"]

    with_arch = inbox_repo.list_threads(db, user_id=user.id, limit=10, offset=0,
                                        include_archived=True)
    assert [t.gmail_id for t in with_arch] == ["g-new", "g-arch", "g-old"]
```

Append to `server/tests/test_inbox_api.py` (reuse its existing authed-client fixture pattern):

```python
def test_inbox_serializer_carries_flags(client, seeded_user_with_thread):
    # follow the file's existing seeding pattern; assert response shape only
    r = client.get("/api/inbox")
    assert r.status_code == 200
    t = r.json()["threads"][0]
    assert t["is_archived"] is False
    assert "is_unread" in (t["recent_message"] or {"is_unread": None})
```

(Adapt fixture names to what `test_inbox_api.py` actually defines — mirror an existing test in that file.)

- [ ] **Step 2: Verify failure**

Run: `cd server && uv run pytest tests/test_inbox_repo.py tests/test_inbox_api.py -q 2>&1 | tail -5`
Expected: FAIL (unexpected kwarg `include_archived`; missing `is_archived` key).

- [ ] **Step 3: Implement repo + API**

Replace `list_threads` in `server/app/inbox/inbox_repo.py`:

```python
def list_threads(
    db: Session, *, user_id: str, limit: int, offset: int,
    include_archived: bool = False,
) -> list[InboxThread]:
    """Threads for the user, most-recently-active first (indexed
    last_activity_at sort — no join). Archived threads are hidden unless
    asked for; they remain queryable because tasks may reference them."""
    stmt = (
        select(InboxThread)
        .where(InboxThread.user_id == user_id)
        .order_by(InboxThread.last_activity_at.desc().nulls_last())
        .limit(limit)
        .offset(offset)
    )
    if not include_archived:
        stmt = stmt.where(InboxThread.is_archived == False)  # noqa: E712
    return list(db.execute(stmt).scalars().all())
```

In `server/app/api/inbox.py`:
- `_serialize_message` gains `"is_unread": msg.is_unread,` in the returned dict.
- `_serialize_thread` gains `"is_archived": thread.is_archived,`.
- `list_inbox` gains a param `include_archived: bool = Query(default=False)` and passes it: `inbox_repo.list_threads(db, user_id=user.id, limit=limit, offset=offset, include_archived=include_archived)`.

- [ ] **Step 4: Run tests, fix fallout**

Run: `cd server && uv run pytest -q 2>&1 | tail -5`
Expected: all pass. If existing `list_threads` tests asserted join-based ordering, they should still pass — `last_activity_at` reproduces the same order (Task 2 maintains it; migration backfills it).

- [ ] **Step 5: Commit**

```bash
git add server/app/inbox/inbox_repo.py server/app/api/inbox.py server/tests/test_inbox_repo.py server/tests/test_inbox_api.py
git commit -m "feat(inbox): archived filter, last_activity_at sort, serializer flags"
```

---

### Task 4: Full sync becomes a reconciling upsert (no wipe)

**Files:**
- Modify: `server/app/workers/gmail_sync.py:204-266` (`full_sync_inbox`)
- Modify: `server/app/inbox/inbox_repo.py:178-186` (`clear_user_inbox` docstring only)
- Test: `server/tests/test_tasks.py` (or the file where `full_sync_inbox` is currently exercised — `rg "full_sync_inbox" server/tests/`)

**Interfaces:**
- Consumes: Task 2's pointer maintenance (`last_activity_at`), Task 3's archived semantics.
- Produces: `full_sync_inbox(db, *, user) -> list[str]` — same signature, new semantics: upserts the newest-200 listing, never deletes rows, and marks stored non-archived threads that vanished from the listed window as `is_archived=True` (their internal ids are included in the returned list so SSE consumers refresh them).

- [ ] **Step 1: Write failing test**

Add to the test file that already fakes a gmail client for full sync (find via `rg -l "full_sync" server/tests/`); follow its existing fake-gmail pattern:

```python
def test_full_sync_reconciles_instead_of_wiping(db, fake_gmail_two_threads, user):
    """Pre-existing thread NOT in the new listing but older than the listed
    window must survive untouched; one inside the window but absent from the
    listing must be marked archived (it left the inbox while the cursor was
    dead); listed threads upsert idempotently."""
    # seed: old thread (activity 100), recent-but-gone thread (activity 5000)
    # fake listing returns threads with internal dates 4000..6000
    ids = gmail_sync.full_sync_inbox(db, user=user)

    old = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-ancient")).scalar_one()
    assert old.is_archived is False          # outside window: untouched

    gone = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-gone")).scalar_one()
    assert gone.is_archived is True          # inside window, absent from listing
    assert gone.id in ids                    # published so clients evict it

    # no rows were deleted
    assert db.execute(select(func.count(InboxThread.id)).where(
        InboxThread.user_id == user.id)).scalar_one() >= 4
```

Write the seeding + fake listing concretely against that file's existing helpers; the assertions above are the contract.

- [ ] **Step 2: Verify failure**

Run: `cd server && uv run pytest tests/test_tasks.py -q 2>&1 | tail -5`
Expected: FAIL (`g-gone` not archived; or rows were wiped).

- [ ] **Step 3: Implement**

In `full_sync_inbox` (`server/app/workers/gmail_sync.py`):

1. Delete the wipe block:

```python
    # DELETE these lines:
    # inbox_repo.clear_user_inbox(db, user_id=user.id)
    # db.flush()
```

2. After the `internal_ids = [...]` upsert loop, add the reconcile step (before the `max_history_id` walk):

```python
    # Reconcile: a stored, non-archived thread whose activity falls inside the
    # window we just listed but which Gmail no longer returns has left the
    # inbox while our cursor was dead (archived/deleted remotely). Mark it
    # archived — never delete; task evidence may reference it. Threads older
    # than the listed window are out of scope for this listing and untouched.
    if parsed_list:
        listed_gmail_ids = {p.gmail_thread_id for p in parsed_list}
        window_min = min(p.recent_internal_date for p in parsed_list)
        stale = db.execute(
            select(InboxThread).where(
                InboxThread.user_id == user.id,
                InboxThread.is_archived == False,  # noqa: E712
                InboxThread.gmail_id.not_in(listed_gmail_ids),
                InboxThread.last_activity_at >= window_min,
            )
        ).scalars().all()
        for t in stale:
            t.is_archived = True
            internal_ids.append(t.id)
        if stale:
            log.info("full_sync_inbox: user=%s archived %d threads absent from listing",
                     user.id, len(stale))
```

3. Update the function docstring: full sync is a reconciling upsert; wipe removed because task tables (Phase 2) FK onto `inbox_threads.id` and `HistoryGoneError` recovery must not orphan evidence.

4. In `inbox_repo.clear_user_inbox`, update the docstring: "Used only for account deletion. Sync paths must never call this — task evidence FKs onto these rows."

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest -q 2>&1 | tail -5`
Expected: all pass. Existing full-sync tests asserting the wipe must be updated to assert reconciliation instead (same file — change assertions, keep scenarios).

- [ ] **Step 5: Commit**

```bash
git add server/app/workers/gmail_sync.py server/app/inbox/inbox_repo.py server/tests/
git commit -m "feat(sync): full sync reconciles via upsert; wipe path removed"
```

---

### Task 5: Mirror archive / soft-delete / unread from Gmail history

**Files:**
- Modify: `server/app/workers/gmail_sync.py:93-201` (`fetch_history_records`, `partial_sync_inbox`)
- Test: `server/tests/test_partial_sync.py`

**Interfaces:**
- Consumes: Task 2's `recompute_thread_pointers`, Task 1 columns.
- Produces: `fetch_history_records` requests `historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]`; `partial_sync_inbox` handles all four record shapes and returns internal ids for every touched thread.

- [ ] **Step 1: Write failing tests**

Append to `server/tests/test_partial_sync.py` (reuse its existing fake-gmail/user seeding pattern; seed one thread `g-t1` with two messages `g-m1`(date 100), `g-m2`(date 200)):

```python
def test_partial_sync_soft_deletes_message_and_recomputes(db, user, seeded_thread):
    records = [{"messagesDeleted": [{"message": {"id": "g-m2", "threadId": "g-t1"}}]}]
    ids = gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                        new_history_id="99")
    m2 = db.execute(select(InboxMessage).where(
        InboxMessage.user_id == user.id, InboxMessage.gmail_id == "g-m2")).scalar_one()
    assert m2.is_deleted is True            # soft, row survives
    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()
    assert t.last_activity_at == 100        # pointer recomputed past the deletion
    assert t.id in ids


def test_partial_sync_mirrors_archive_and_unread(db, user, seeded_thread):
    records = [
        {"labelsRemoved": [{"message": {"id": "g-m1", "threadId": "g-t1"},
                            "labelIds": ["INBOX"]}]},
        {"labelsAdded":   [{"message": {"id": "g-m1", "threadId": "g-t1"},
                            "labelIds": ["UNREAD"]}]},
    ]
    ids = gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                        new_history_id="100")
    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()
    assert t.is_archived is True            # INBOX label removed → archived
    m1 = db.execute(select(InboxMessage).where(
        InboxMessage.user_id == user.id, InboxMessage.gmail_id == "g-m1")).scalar_one()
    assert m1.is_unread is True
    assert t.id in ids


def test_partial_sync_unarchives_on_inbox_label_added(db, user, seeded_thread):
    # pre-archive the thread, then deliver labelsAdded INBOX
    t = db.execute(select(InboxThread).where(
        InboxThread.user_id == user.id, InboxThread.gmail_id == "g-t1")).scalar_one()
    t.is_archived = True
    records = [{"labelsAdded": [{"message": {"id": "g-m1", "threadId": "g-t1"},
                                 "labelIds": ["INBOX"]}]}]
    gmail_sync.partial_sync_inbox(db, user=user, history_records=records,
                                  new_history_id="101")
    assert t.is_archived is False
```

- [ ] **Step 2: Verify failure**

Run: `cd server && uv run pytest tests/test_partial_sync.py -q 2>&1 | tail -5`
Expected: FAIL (records ignored — only `messagesAdded` handled today).

- [ ] **Step 3: Implement**

In `fetch_history_records`, widen the request:

```python
            historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
```

(keep `labelId="INBOX"` and the comment about singular/plural.)

In `partial_sync_inbox`, replace the record-walk block (currently only `messagesAdded`) with:

```python
    touched_gmail_ids: set[str] = set()      # need a threads.get + full upsert
    flag_touched_internal_ids: set[str] = set()  # in-place flag updates only

    def _local_thread(gmail_thread_id: str) -> InboxThread | None:
        return db.execute(select(InboxThread).where(
            InboxThread.user_id == user.id,
            InboxThread.gmail_id == gmail_thread_id)).scalar_one_or_none()

    def _local_message(gmail_message_id: str) -> InboxMessage | None:
        return db.execute(select(InboxMessage).where(
            InboxMessage.user_id == user.id,
            InboxMessage.gmail_id == gmail_message_id)).scalar_one_or_none()

    for record in history_records:
        for added in record.get("messagesAdded", []) or []:
            tid = (added.get("message") or {}).get("threadId")
            if tid:
                touched_gmail_ids.add(tid)

        for deleted in record.get("messagesDeleted", []) or []:
            gm_id = (deleted.get("message") or {}).get("id")
            row = _local_message(gm_id) if gm_id else None
            if row is None:
                continue
            # Soft delete: task evidence (Phase 2) must survive Gmail deletions.
            row.is_deleted = True
            thread = db.get(InboxThread, row.thread_id)
            if thread is not None:
                inbox_repo.recompute_thread_pointers(db, thread=thread)
                if thread.recent_message_id is None:
                    thread.is_archived = True  # every message gone → leave the inbox view
                flag_touched_internal_ids.add(thread.id)

        for key, label_present in (("labelsAdded", True), ("labelsRemoved", False)):
            for change in record.get(key, []) or []:
                labels = set(change.get("labelIds", []) or [])
                msg = change.get("message") or {}
                if "INBOX" in labels and msg.get("threadId"):
                    thread = _local_thread(msg["threadId"])
                    if thread is not None:
                        thread.is_archived = not label_present
                        flag_touched_internal_ids.add(thread.id)
                    elif label_present:
                        # INBOX added to a thread we don't hold → ingest it.
                        touched_gmail_ids.add(msg["threadId"])
                if "UNREAD" in labels and msg.get("id"):
                    row = _local_message(msg["id"])
                    if row is not None:
                        row.is_unread = label_present
                        flag_touched_internal_ids.add(row.thread_id)
```

Keep the existing fetch/classify/upsert flow for `touched_gmail_ids`, then merge results before the commit:

```python
    internal_ids = list({*internal_ids, *flag_touched_internal_ids})
```

(The union goes right before the `if new_history_id:` cursor advance; commit and return as today.)

- [ ] **Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_partial_sync.py -q 2>&1 | tail -5` → PASS.
Full suite: `cd server && uv run pytest -q 2>&1 | tail -5` → all pass.

- [ ] **Step 5: Commit**

```bash
git add server/app/workers/gmail_sync.py server/tests/test_partial_sync.py
git commit -m "feat(sync): mirror gmail archive/delete/unread via widened historyTypes"
```

---

### Task 6: `search_repo` + `GET /api/search`

**Files:**
- Create: `server/app/inbox/search_repo.py`
- Create: `server/app/api/search.py`
- Modify: `server/app/main.py` (register router)
- Test: `server/tests/test_search.py`

**Interfaces:**
- Consumes: Task 1's FTS columns (PG) / Task 2's persisted `body_text` (fallback path).
- Produces: `search_repo.search_threads(db, *, user_id: str, q: str, include_archived: bool = False, limit: int = 50, offset: int = 0) -> list[InboxThread]`; route `GET /api/search?q=&page=&limit=&include_archived=` returning `{as_of, page, limit, threads: [...]}` (same thread shape as `/api/inbox`). Task 9 (client) consumes the route.

- [ ] **Step 1: Write failing tests**

Create `server/tests/test_search.py`:

```python
"""search_repo tests exercise the non-Postgres fallback branch (tests run on
SQLite). The Postgres FTS branch shares the same contract and is verified
manually against the dev stack (see plan Task 6 step 5)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from app.db.models import InboxThread, User
from app.inbox import inbox_repo, search_repo


def _mk_user(db) -> User:
    u = User(id=uuid.uuid4().hex, email=f"{uuid.uuid4().hex}@t.co",
             created_at=datetime.now(timezone.utc))
    db.add(u)
    db.flush()
    return u


def _mk_thread(db, user, gid, subject, body, from_addr="a@b.c", date=100):
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id=gid,
                             subject=subject, bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id=gid, gmail_message_id=f"m-{gid}",
        gmail_internal_date=date, gmail_history_id="1",
        to_addr=None, from_addr=from_addr, body_preview=body[:150],
        body_text=body, label_ids=["INBOX"])


def test_search_matches_subject_body_and_sender(db):
    user = _mk_user(db)
    _mk_thread(db, user, "g1", "Stripe onsite invite", "we'd like to invite you onsite")
    _mk_thread(db, user, "g2", "Grocery list", "milk and eggs", from_addr="recruiting@stripe.com")
    _mk_thread(db, user, "g3", "Unrelated", "nothing to see")

    by_subject = search_repo.search_threads(db, user_id=user.id, q="onsite")
    assert {t.gmail_id for t in by_subject} == {"g1"}

    by_sender = search_repo.search_threads(db, user_id=user.id, q="stripe")
    assert {t.gmail_id for t in by_sender} == {"g1", "g2"}


def test_search_is_user_scoped_and_skips_archived(db):
    alice, bob = _mk_user(db), _mk_user(db)
    _mk_thread(db, alice, "ga", "topic zebra", "zebra body")
    _mk_thread(db, bob, "gb", "topic zebra", "zebra body")
    arch = db.execute(select(InboxThread).where(
        InboxThread.user_id == alice.id, InboxThread.gmail_id == "ga")).scalar_one()
    arch.is_archived = True

    assert search_repo.search_threads(db, user_id=alice.id, q="zebra") == []
    assert len(search_repo.search_threads(db, user_id=alice.id, q="zebra",
                                          include_archived=True)) == 1
    assert len(search_repo.search_threads(db, user_id=bob.id, q="zebra")) == 1
```

- [ ] **Step 2: Verify failure**

Run: `cd server && uv run pytest tests/test_search.py -q 2>&1 | tail -5`
Expected: FAIL (`search_repo` module missing).

- [ ] **Step 3: Implement `search_repo`**

Create `server/app/inbox/search_repo.py`:

```python
"""Thread text search. NEVER commits (caller owns the txn).

Two branches on dialect:
 - postgresql: FTS over the 0006 generated tsvector columns
   (inbox_messages.search_tsv, inbox_threads.subject_tsv) ranked by
   ts_rank_cd then recency. websearch_to_tsquery gives users quotes/-/OR.
 - everything else (SQLite tests): ILIKE substring over subject / sender /
   body_text, recency-ordered. Same contract, no ranking.
"""

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session
from app.db.models import InboxMessage, InboxThread


def search_threads(
    db: Session, *, user_id: str, q: str,
    include_archived: bool = False, limit: int = 50, offset: int = 0,
) -> list[InboxThread]:
    if db.get_bind().dialect.name == "postgresql":
        return _search_pg(db, user_id=user_id, q=q,
                          include_archived=include_archived, limit=limit, offset=offset)
    return _search_fallback(db, user_id=user_id, q=q,
                            include_archived=include_archived, limit=limit, offset=offset)


def _search_pg(db, *, user_id, q, include_archived, limit, offset) -> list[InboxThread]:
    rows = db.execute(text("""
        SELECT t.id
        FROM inbox_threads t
        LEFT JOIN inbox_messages m
               ON m.thread_id = t.id AND m.is_deleted = false
        WHERE t.user_id = :user_id
          AND (:include_archived OR t.is_archived = false)
          AND (t.subject_tsv @@ websearch_to_tsquery('english', :q)
               OR m.search_tsv @@ websearch_to_tsquery('english', :q)
               OR m.from_addr ILIKE '%' || :q || '%')
        GROUP BY t.id, t.last_activity_at
        ORDER BY COALESCE(MAX(ts_rank_cd(m.search_tsv,
                                         websearch_to_tsquery('english', :q))), 0) DESC,
                 t.last_activity_at DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """), {"user_id": user_id, "q": q, "include_archived": include_archived,
           "limit": limit, "offset": offset}).all()
    ids = [r[0] for r in rows]
    if not ids:
        return []
    by_id = {t.id: t for t in db.execute(
        select(InboxThread).where(InboxThread.id.in_(ids))).scalars()}
    return [by_id[i] for i in ids if i in by_id]  # preserve rank order


def _search_fallback(db, *, user_id, q, include_archived, limit, offset) -> list[InboxThread]:
    like = f"%{q}%"
    stmt = (
        select(InboxThread).distinct()
        .outerjoin(InboxMessage, (InboxMessage.thread_id == InboxThread.id)
                   & (InboxMessage.is_deleted == False))  # noqa: E712
        .where(InboxThread.user_id == user_id)
        .where(or_(InboxThread.subject.ilike(like),
                   InboxMessage.from_addr.ilike(like),
                   InboxMessage.body_text.ilike(like)))
        .order_by(InboxThread.last_activity_at.desc().nulls_last())
        .limit(limit).offset(offset)
    )
    if not include_archived:
        stmt = stmt.where(InboxThread.is_archived == False)  # noqa: E712
    return list(db.execute(stmt).scalars().all())
```

- [ ] **Step 4: Implement the route + register it**

Create `server/app/api/search.py`:

```python
"""GET /api/search — thread text search for the HUD EDA loop.

Response reuses the /api/inbox thread shape so the client renders results
with the existing InboxList row component.
"""

import logging
import time
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.db.models import User
from app.db.session import get_db
from app.deps import get_current_user
from app.api.inbox import _serialize_thread, DEFAULT_LIMIT, MAX_LIMIT
from app.inbox import search_repo

router = APIRouter(prefix="/api", tags=["search"])
log = logging.getLogger(__name__)


@router.get("/search")
def search(
    q: str = Query(min_length=1, max_length=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    page: int = Query(default=1, ge=1),
    include_archived: bool = Query(default=False),
) -> dict:
    offset = (page - 1) * limit
    threads = search_repo.search_threads(
        db, user_id=user.id, q=q, include_archived=include_archived,
        limit=limit, offset=offset)
    log.info("search: user=%s q_len=%d → %d threads", user.id, len(q), len(threads))
    return {
        "as_of": int(time.time() * 1000),
        "page": page,
        "limit": limit,
        "threads": [_serialize_thread(db, user.id, t) for t in threads],
    }
```

In `server/app/main.py`, add next to the other router imports/includes:

```python
from app.api.search import router as search_router
```

```python
app.include_router(search_router)
```

Add route tests to `server/tests/test_search.py` (mirror `test_inbox_api.py`'s authed-client fixture pattern): 200 with results for an owned match, 422 on missing `q`, and results empty for another user's data.

- [ ] **Step 5: Run tests + manual PG check**

Run: `cd server && uv run pytest tests/test_search.py -q 2>&1 | tail -5` → PASS.
Full suite → all pass.
Manual (PG branch — once, before phase close): `scripts/dev.sh`, then
`curl -s 'http://localhost:8000/api/search?q=test' -H "Cookie: <session>"` and confirm ranked results; verify `EXPLAIN` uses `ix_inbox_messages_search_tsv`.

- [ ] **Step 6: Commit**

```bash
git add server/app/inbox/search_repo.py server/app/api/search.py server/app/main.py server/tests/test_search.py
git commit -m "feat(search): dialect-aware search_repo + GET /api/search"
```

---

### Task 7: Reclassify + draft preview read Postgres (no Gmail refetch)

**Files:**
- Modify: `server/app/inbox/inbox_repo.py` (new `load_parsed_threads`)
- Modify: `server/app/workers/tasks.py:305-351` (`_score_all`), `:422-480` (`_reclassify_all`), `draft_preview_bucket` call site
- Test: `server/tests/test_tasks.py`, `server/tests/test_draft_preview_task.py`, `server/tests/test_inbox_repo.py`

**Interfaces:**
- Consumes: Task 2's persisted `body_text`.
- Produces: `inbox_repo.load_parsed_threads(db, *, user_id: str, internal_ids: list[str] | None = None) -> list[tuple[str, str | None, ParsedThread]]` (internal_id, bucket_id, parsed; non-deleted messages ascending by date; `body_text` falls back to `body_preview` for pre-migration rows). `_score_all(db, *, user_id, candidates, name, description)` — gmail client parameter REMOVED.

- [ ] **Step 1: Write failing tests**

Append to `server/tests/test_inbox_repo.py`:

```python
def test_load_parsed_threads_reconstructs_from_rows(db):
    user = _mk_user(db)
    inbox_repo.upsert_thread(db, user_id=user.id, gmail_thread_id="glp",
                             subject="subj", bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id=user.id, gmail_thread_id="glp", gmail_message_id="m-a",
        gmail_internal_date=200, gmail_history_id="1", to_addr=None,
        from_addr="x@y.z", body_preview="prev", body_text="full body")
    inbox_repo.upsert_message(  # pre-migration shape: no body_text
        db, user_id=user.id, gmail_thread_id="glp", gmail_message_id="m-b",
        gmail_internal_date=100, gmail_history_id="1", to_addr=None,
        from_addr="x@y.z", body_preview="only preview")

    triples = inbox_repo.load_parsed_threads(db, user_id=user.id)
    assert len(triples) == 1
    internal_id, bucket_id, parsed = triples[0]
    assert bucket_id is None
    assert parsed.gmail_thread_id == "glp"
    assert [m.gmail_message_id for m in parsed.messages] == ["m-b", "m-a"]  # ascending
    assert parsed.messages[1].body_text == "full body"
    assert parsed.messages[0].body_text == "only preview"  # fallback
    assert parsed.recent_internal_date == 200
```

In `server/tests/test_tasks.py` / `test_draft_preview_task.py`: delete/replace the `gmail.users().threads().get` mocks feeding `_reclassify_all` and `_score_all`; the reclassify test seeds bodies via `upsert_message(body_text=...)` and asserts identical classification outcomes with NO gmail client involved (the fake gmail object should now raise if `.users()` is called from these paths — assert it isn't).

- [ ] **Step 2: Verify failure**

Run: `cd server && uv run pytest tests/test_inbox_repo.py tests/test_tasks.py tests/test_draft_preview_task.py -q 2>&1 | tail -5`
Expected: FAIL (`load_parsed_threads` missing).

- [ ] **Step 3: Implement `load_parsed_threads`**

In `server/app/inbox/inbox_repo.py` (add import `from app.gmail.parser import ParsedMessage, ParsedThread` at the top):

```python
def load_parsed_threads(
    db: Session, *, user_id: str, internal_ids: list[str] | None = None,
) -> list[tuple[str, str | None, ParsedThread]]:
    """Rebuild ParsedThreads from stored rows so LLM paths never refetch
    Gmail (chosen-architecture §5.1 self-sufficiency). Returns
    (internal_id, bucket_id, parsed) triples; soft-deleted messages are
    excluded; body_text falls back to body_preview for pre-0006 rows that
    haven't been re-touched by sync yet. Threads with no usable messages
    are skipped."""
    stmt = select(InboxThread).where(InboxThread.user_id == user_id)
    if internal_ids is not None:
        if not internal_ids:
            return []
        stmt = stmt.where(InboxThread.id.in_(internal_ids))
    out: list[tuple[str, str | None, ParsedThread]] = []
    for t in db.execute(stmt).scalars().all():
        msgs = db.execute(
            select(InboxMessage)
            .where(InboxMessage.thread_id == t.id,
                   InboxMessage.is_deleted == False)  # noqa: E712
            .order_by(InboxMessage.gmail_internal_date.asc())
        ).scalars().all()
        parsed_msgs = [
            ParsedMessage(
                gmail_message_id=m.gmail_id, gmail_thread_id=m.gmail_thread_id,
                gmail_internal_date=m.gmail_internal_date,
                gmail_history_id=m.gmail_history_id,
                subject=t.subject, from_addr=m.from_addr, to_addr=m.to_addr,
                body_text=m.body_text or m.body_preview or "",
                body_preview=m.body_preview or "",
                label_ids=list(m.labels or []),
            )
            for m in msgs
        ]
        if not parsed_msgs:
            continue
        out.append((t.id, t.bucket_id, ParsedThread(
            gmail_thread_id=t.gmail_id, subject=t.subject,
            recent_internal_date=parsed_msgs[-1].gmail_internal_date,
            messages=parsed_msgs)))
    return out
```

- [ ] **Step 4: Rewrite the two consumers**

`_reclassify_all` in `server/app/workers/tasks.py` — replace the Gmail fetch loop:

```python
def _reclassify_all(db, *, user) -> list[str]:
    """Reclassify every stored thread from Postgres bodies (0006+). The old
    per-thread gmail.threads.get loop (~200ms each) is gone — reclassify of a
    200-thread inbox is now LLM-bound."""
    triples = inbox_repo.load_parsed_threads(db, user_id=user.id)
    if not triples:
        log.info("reclassify._reclassify_all: user=%s no threads", user.id)
        return []

    buckets = bucket_repo.list_active(db, user_id=user.id)
    threads = [p for _, _, p in triples]
    current = [b for _, b, _ in triples]
    log.info("reclassify._reclassify_all: user=%s classifying %d threads against %d buckets",
             user.id, len(threads), len(buckets))
    new_bucket_ids = classify(threads, buckets, current)  # Task 8 adds user_id=

    changed: list[str] = []
    for (internal_id, old_bucket, _), new_bucket in zip(triples, new_bucket_ids):
        if new_bucket == old_bucket:
            continue
        thread_row = db.get(InboxThread, internal_id)
        if thread_row is None:
            continue
        thread_row.bucket_id = new_bucket
        changed.append(internal_id)
    db.commit()
    log.info("reclassify._reclassify_all: user=%s %d threads moved buckets",
             user.id, len(changed))
    return changed
```

Add the imports at the top of `tasks.py` if missing: `from app.inbox import inbox_repo`.

`_score_all` — new signature and body:

```python
def _score_all(db, *, user_id: str, candidates: list[dict], name: str, description: str) -> list[dict]:
    """Score candidates from Postgres bodies (0006+) in parallel under the
    shared LLM semaphore. The sequential gmail refetch is gone."""
    triples = inbox_repo.load_parsed_threads(
        db, user_id=user_id, internal_ids=[c["thread_id"] for c in candidates])
    parsed_by_id = {internal_id: parsed for internal_id, _, parsed in triples}
    pairs = [(c, parsed_by_id[c["thread_id"]]) for c in candidates
             if c["thread_id"] in parsed_by_id]

    s = get_settings()

    async def _score_one(parsed):
        text = await llm_client.call_messages(
            model=s.llm_classify_model,
            system=score_thread.SYSTEM_PROMPT,
            user=score_thread.build_user_message(
                thread_str=thread_to_string(parsed), name=name, description=description),
        )
        return score_thread.parse_response(text)

    async def _all():
        return await asyncio.gather(*[_score_one(p) for _, p in pairs])

    parsed_results = llm_client.run_in_loop(_all())

    out = []
    for (c, _), result in zip(pairs, parsed_results):
        if not result:
            continue
        out.append({
            "thread_id": c["thread_id"], "subject": c["subject"], "sender": c["sender"],
            "score": result["score"], "rationale": result["rationale"],
            "snippet": result["snippet"],
        })
    return out
```

In `draft_preview_bucket`, replace:

```python
        gmail = get_gmail_client(db, user)
        scored = _score_all(gmail, candidates=candidates, name=name, description=description)
```

with:

```python
        scored = _score_all(db, user_id=user_id, candidates=candidates,
                            name=name, description=description)
```

Remove now-unused imports from `tasks.py` if nothing else uses them (`assemble_thread`; keep `get_gmail_client` — `poll_new_messages` still uses it).

- [ ] **Step 5: Run tests**

Run: `cd server && uv run pytest -q 2>&1 | tail -5` → all pass, and the updated tests prove no `threads.get` calls occur in reclassify/preview paths.

- [ ] **Step 6: Commit**

```bash
git add server/app/inbox/inbox_repo.py server/app/workers/tasks.py server/tests/
git commit -m "perf(llm-paths): reclassify + draft preview read Postgres bodies, drop gmail refetch"
```

---

### Task 8: `llm_calls` instrumentation in the LLM client

**Files:**
- Create: `server/app/llm/metrics.py`
- Modify: `server/app/llm/client.py` (`call_messages`)
- Modify: `server/app/llm/classify.py`, `server/app/workers/gmail_sync.py:69-91` (`_classify_batch`), `server/app/workers/tasks.py` (`_score_all`, `_reclassify_all` classify call)
- Test: `server/tests/test_llm_metrics.py`, `server/tests/test_llm_client.py`

**Interfaces:**
- Consumes: `LlmCall` model (Task 1).
- Produces: `metrics.record_call(*, stage, model, user_id=None, task_id=None, input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None, cost_usd=None, ttft_ms=None, duration_ms, outcome)` (module attr `SessionLocal`, monkeypatchable, NEVER raises); `call_messages(..., stage: str = "unknown", user_id: str | None = None)`; `classify(threads, buckets, current_bucket_ids, *, user_id=None)`.

- [ ] **Step 1: Write failing tests**

Create `server/tests/test_llm_metrics.py`:

```python
import uuid
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db.models import Base, LlmCall
from app.llm import client, metrics


def _static_engine():
    # StaticPool + check_same_thread=False: one shared connection usable from
    # the asyncio.to_thread worker that record_call runs on.
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True,
                        poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


def test_record_call_writes_row(monkeypatch):
    eng = _static_engine()
    monkeypatch.setattr(metrics, "SessionLocal",
                        sessionmaker(bind=eng, future=True))
    metrics.record_call(stage="classify", model="anthropic/claude-haiku-4-5",
                        user_id="u1", input_tokens=100, output_tokens=20,
                        cost_usd=0.0003, duration_ms=250, outcome="success")
    with sessionmaker(bind=eng, future=True)() as s:
        row = s.execute(select(LlmCall)).scalar_one()
        assert row.stage == "classify"
        assert row.input_tokens == 100
        assert row.outcome == "success"
        assert row.duration_ms == 250


def test_record_call_never_raises(monkeypatch):
    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")
    monkeypatch.setattr(metrics, "SessionLocal", _Boom())
    metrics.record_call(stage="classify", model="m", duration_ms=1, outcome="success")
    # reaching here without an exception IS the assertion
```

Append to `server/tests/test_llm_client.py`:

```python
def test_call_messages_records_success_metrics(monkeypatch):
    client._ensure_initialized()
    calls: list[dict] = []
    from app.llm import metrics
    monkeypatch.setattr(metrics, "record_call", lambda **kw: calls.append(kw))

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 20
        prompt_tokens_details = None
        cost = 0.0003
    class _Msg: content = "hi"
    class _Choice: message = _Msg()
    class _Resp:
        choices = [_Choice()]
        usage = _Usage()
    class _Create:
        async def create(self, **kw): return _Resp()
    class _Chat: completions = _Create()
    class _C: chat = _Chat()
    client._state["client"] = _C()

    out = client.run_in_loop(client.call_messages(
        model="m", system="s", user="u", stage="classify", user_id="u1"))
    assert out == "hi"
    assert len(calls) == 1
    assert calls[0]["stage"] == "classify"
    assert calls[0]["user_id"] == "u1"
    assert calls[0]["input_tokens"] == 100
    assert calls[0]["output_tokens"] == 20
    assert calls[0]["cost_usd"] == 0.0003
    assert calls[0]["outcome"] == "success"
    assert calls[0]["duration_ms"] >= 0


def test_call_messages_records_error_metrics(monkeypatch):
    client._ensure_initialized()
    calls: list[dict] = []
    from app.llm import metrics
    monkeypatch.setattr(metrics, "record_call", lambda **kw: calls.append(kw))

    class _Boom:
        async def create(self, **kw): raise RuntimeError("nope")
    class _Chat: completions = _Boom()
    class _C: chat = _Chat()
    client._state["client"] = _C()

    assert client.run_in_loop(client.call_messages(model="m", system="s", user="u")) == ""
    assert len(calls) == 1
    assert calls[0]["outcome"] == "error"
```

- [ ] **Step 2: Verify failure**

Run: `cd server && uv run pytest tests/test_llm_metrics.py tests/test_llm_client.py -q 2>&1 | tail -5`
Expected: FAIL (`app.llm.metrics` missing).

- [ ] **Step 3: Implement `metrics.py`**

Create `server/app/llm/metrics.py`:

```python
"""Persist per-LLM-call metrics to llm_calls (VISION: persisted, not logged).

record_call is deliberately fire-and-forget: it opens its own short session
(module-attr SessionLocal, monkeypatchable like workers/tasks.py) and
swallows every exception — a metrics failure must never fail an LLM call.
Called from llm/client.py via asyncio.to_thread so the sync DB write never
blocks the LLM event loop.
"""

import logging
import uuid
from datetime import datetime, timezone

from app.db.session import SessionLocal as _AppSessionLocal
from app.db.models import LlmCall

SessionLocal = _AppSessionLocal
log = logging.getLogger(__name__)


def record_call(
    *, stage: str, model: str, user_id: str | None = None,
    task_id: str | None = None, input_tokens: int | None = None,
    output_tokens: int | None = None, cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None, cost_usd: float | None = None,
    ttft_ms: int | None = None, duration_ms: int, outcome: str,
) -> None:
    try:
        db = SessionLocal()
        try:
            db.add(LlmCall(
                id=uuid.uuid4().hex, user_id=user_id, task_id=task_id,
                stage=stage, model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=cost_usd, ttft_ms=ttft_ms,
                duration_ms=duration_ms, outcome=outcome,
                created_at=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("llm.metrics: record_call failed (ignored)")
```

- [ ] **Step 4: Instrument `call_messages`**

In `server/app/llm/client.py`: add `import time` and `from app.llm import metrics` at the top; replace `call_messages`:

```python
async def call_messages(*, model: str, system: str, user: str, max_tokens: int = 1024,
                        stage: str = "unknown", user_id: str | None = None) -> str:
    _ensure_initialized()
    sem: asyncio.Semaphore = _state["sem"]
    client: AsyncOpenAI = _state["client"]
    async with sem:
        t0 = time.monotonic()
        try:
            # OpenAI-format: the Anthropic top-level `system` becomes a
            # system-role message; response is a single string, not blocks.
            # extra_body usage.include asks OpenRouter to attach cost + cached
            # token counts to resp.usage.
            resp = await client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                extra_body={"usage": {"include": True}},
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            usage = getattr(resp, "usage", None)
            details = getattr(usage, "prompt_tokens_details", None) if usage else None
            await asyncio.to_thread(
                metrics.record_call,
                stage=stage, model=model, user_id=user_id,
                input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
                output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
                cache_read_tokens=getattr(details, "cached_tokens", None) if details else None,
                cost_usd=getattr(usage, "cost", None) if usage else None,
                duration_ms=duration_ms, outcome="success",
            )
            return resp.choices[0].message.content or ""
        except Exception:
            duration_ms = int((time.monotonic() - t0) * 1000)
            log.exception("openrouter chat.completions.create failed")
            await asyncio.to_thread(
                metrics.record_call, stage=stage, model=model, user_id=user_id,
                duration_ms=duration_ms, outcome="error",
            )
            return ""
```

- [ ] **Step 5: Thread `stage`/`user_id` from call sites**

- `server/app/llm/classify.py`: `_classify_one` gains `user_id: str | None` param and passes `stage="classify", user_id=user_id` to `call_messages`; `classify` gains keyword-only `user_id: str | None = None` and forwards it in the gather comprehension.
- `server/app/workers/gmail_sync.py` `_classify_batch`: final line becomes `return classify(parsed_list, buckets, current, user_id=user_id)`.
- `server/app/workers/tasks.py`: `_reclassify_all`'s classify call becomes `classify(threads, buckets, current, user_id=user.id)`; `_score_all`'s `_score_one` adds `stage="score", user_id=user_id` to its `call_messages` call.

- [ ] **Step 6: Run tests**

Run: `cd server && uv run pytest tests/test_llm_metrics.py tests/test_llm_client.py tests/test_classify.py -q 2>&1 | tail -5` → PASS.
Full suite → all pass (existing classify tests are unaffected: new params are optional).

- [ ] **Step 7: Commit**

```bash
git add server/app/llm/metrics.py server/app/llm/client.py server/app/llm/classify.py server/app/workers/gmail_sync.py server/app/workers/tasks.py server/tests/test_llm_metrics.py server/tests/test_llm_client.py
git commit -m "feat(llm): persist per-call metrics to llm_calls from the client choke point"
```

---

### Task 9: Client — archived eviction + inbox search box

**Files:**
- Modify: `client/src/lib/api.ts` (types + `searchInbox`)
- Modify: `client/src/pages/inbox/useInbox.tsx` (`applyThreadUpdates`)
- Modify: `client/src/pages/Home.tsx` (search bar + results view)

**Interfaces:**
- Consumes: `GET /api/search` (Task 6), `is_archived` in thread JSON (Task 3).
- Produces: `InboxThread.is_archived: boolean`; `searchInbox(q: string): Promise<InboxPage>`.

- [ ] **Step 1: Extend `api.ts`**

Add to the `InboxThread` type: `is_archived: boolean`. Add to `InboxMessage`: `is_unread?: boolean`. Add after `getInbox`:

```typescript
export function searchInbox(q: string): Promise<InboxPage> {
  const params = new URLSearchParams({ q })
  return getJSON<InboxPage>(`/api/search?${params.toString()}`)
}
```

- [ ] **Step 2: Evict archived threads in `useInbox.applyThreadUpdates`**

In `client/src/pages/inbox/useInbox.tsx`, inside `applyThreadUpdates`, after the LWW `accepted` loop, replace the final two `set*` calls with:

```typescript
    // Archived threads (mirrored from Gmail) leave the list instead of merging.
    const archived = accepted.filter(t => t.is_archived)
    const live = accepted.filter(t => !t.is_archived)
    if (archived.length > 0) {
      const drop = new Set(archived.map(t => t.id))
      for (const t of archived) delete lastInternalDate.current[t.id]
      setDisplayLayer(prev => {
        const n = { ...prev }; for (const t of archived) delete n[t.id]; return n
      })
      setIdLayer(prev => prev.filter(id => !drop.has(id)))
    }
    if (live.length === 0) return
    setDisplayLayer(prev => { const n = { ...prev }; for (const t of live) n[t.id] = t; return n })
    setIdLayer(prev => {
      const merged = new Set(prev); for (const t of live) merged.add(t.id)
      return [...merged].sort((a, b) =>
        (lastInternalDate.current[b] ?? 0) - (lastInternalDate.current[a] ?? 0))
    })
```

- [ ] **Step 3: Search bar in `Home.tsx`**

Add imports: `searchInbox, type InboxThread` from `../lib/api`. Add state + debounced fetch inside `Home` (after the `useInboxSse` line):

```typescript
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<InboxThread[] | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)

  // Debounced server search: /api/search (Postgres FTS). Empty query exits
  // search mode and restores the normal inbox list.
  useEffect(() => {
    const q = searchQuery.trim()
    if (!q) { setSearchResults(null); setSearchError(null); return }
    const t = setTimeout(async () => {
      try {
        const r = await searchInbox(q)
        setSearchResults(r.threads); setSearchError(null)
      } catch (e: any) {
        setSearchError(String(e?.message ?? e))
      }
    }, 300)
    return () => clearTimeout(t)
  }, [searchQuery])
```

Insert the bar between `<SecondaryHeader …/>` and `<main>`:

```tsx
      <div style={{ padding: '8px 24px', borderBottom: '1px solid #eee' }}>
        <input
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="search your inbox…"
          style={{ width: 360, maxWidth: '100%', padding: '6px 10px', fontSize: 14 }}
        />
        {searchResults !== null && (
          <span style={{ marginLeft: 12, fontSize: 12, color: '#888' }}>
            {searchResults.length} result{searchResults.length === 1 ? '' : 's'}
            <button onClick={() => setSearchQuery('')}
                    style={{ marginLeft: 8, fontSize: 12 }}>clear</button>
          </span>
        )}
      </div>
```

And branch `<main>`'s body: when `searchResults !== null`, render search mode instead of the inbox list:

```tsx
      <main>
        {searchResults !== null ? (
          <>
            {searchError && <div style={{ color: '#8a1c25', padding: 16 }}>search error: {searchError}</div>}
            <InboxList threads={searchResults} bucketsById={bucketsById} />
          </>
        ) : (
          <>
            {inbox.error && <div style={{ color: '#8a1c25', padding: 16 }}>error: {inbox.error}</div>}
            {!inbox.error && inbox.loading && <div style={{ padding: 24 }}>loading…</div>}
            {!inbox.loading && <InboxList threads={inbox.pageThreads} bucketsById={bucketsById} />}
            {inbox.more === false && (
              <div style={{ padding: 12, fontSize: 12, color: '#888', textAlign: 'center' }}>
                (end of inbox history)
              </div>
            )}
          </>
        )}
      </main>
```

- [ ] **Step 4: Typecheck/build**

Run: `cd client && bun run build 2>&1 | tail -5`
Expected: build succeeds (tsc clean). Note: `InboxList` shows "syncing your inbox…" for an empty array — acceptable for zero-result searches in Phase 0 (HUD phase revisits empty states).

- [ ] **Step 5: End-to-end check**

Run `scripts/dev.sh`; in the browser: type a word known to be in an email body → results appear; clear → inbox restores; archive a thread in Gmail → it disappears from the open tab within ~35s.

- [ ] **Step 6: Commit**

```bash
git add client/src/lib/api.ts client/src/pages/inbox/useInbox.tsx client/src/pages/Home.tsx
git commit -m "feat(client): inbox search box + archived-thread eviction"
```

---

### Task 10: Refresh reference docs + stamps

**Files:**
- Modify: `reference/INBOX_SYNC_INDEX.md`, `reference/WORKERS_INDEX.md`, `reference/MANIFEST.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Update `INBOX_SYNC_INDEX.md`**

Reflect: full sync = reconciling upsert (no wipe; `clear_user_inbox` = account deletion only); widened `historyTypes` + archive/soft-delete/unread mirroring; `body_text`/`labels`/`is_unread`/`is_deleted` + `is_archived`/`last_activity_at` columns; `recompute_thread_pointers`; `list_threads` sorted by `last_activity_at` + archived filter; `load_parsed_threads`; `GET /api/search` + `search_repo`; the "Cursor expiry → full sync (wipe + repopulate)" gotcha rewritten.

- [ ] **Step 2: Update `WORKERS_INDEX.md`**

Reflect: `_reclassify_all`/`_score_all` read Postgres (no Gmail refetch; `_score_all(db, *, user_id, candidates, ...)`); `llm_calls` metrics via `app/llm/metrics.py` from `call_messages`; `classify(..., user_id=)`.

- [ ] **Step 3: Re-stamp per repo rule**

Commit all Phase-0 code first (Tasks 1–9 are committed), then set each doc's top stamp and the matching MANIFEST rows to the current commit:

```bash
git log -1 --format=%h   # → <sha>
# set '<!-- stamp: <sha> (main) | <today> -->' in both docs + MANIFEST rows
git add reference/INBOX_SYNC_INDEX.md reference/WORKERS_INDEX.md reference/MANIFEST.md
git commit -m "docs(reference): re-index sync/workers for phase 0 data floor"
```

- [ ] **Step 4: Final verification**

Run: `cd server && uv run pytest -q 2>&1 | tail -5` → all green.
Run: `cd server && uv run alembic upgrade head` against the docker Postgres (`scripts/dev.sh` stack) → applies cleanly, FTS objects created (`\d inbox_messages` shows `search_tsv`).
