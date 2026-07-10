from datetime import datetime
from sqlalchemy import (Boolean, String, Text, DateTime, ForeignKey, BigInteger,
                        Integer, Float, JSON, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    gmail_refresh_token: Mapped[str | None] = mapped_column(Text)  # encrypted at rest
    gmail_access_token: Mapped[str | None] = mapped_column(Text)   # encrypted at rest
    gmail_access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_last_history_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user")


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # secrets.token_urlsafe(32) -> 43 chars
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")


class InboxThread(Base):
    __tablename__ = "inbox_threads"
    # Prevents duplicate rows when concurrent Celery tasks (beat + user-triggered reload,
    # retries after transient errors, etc.) race to insert the same thread for a user.
    __table_args__ = (
        UniqueConstraint("user_id", "gmail_id", name="uq_inbox_threads_user_gmail"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    gmail_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    bucket_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id"))
    # recent_message_id can't FK at row-create time (chicken/egg); it's a soft pointer.
    recent_message_id: Mapped[str | None] = mapped_column(String(36))
    # Mirrors Gmail INBOX-label removal (archive). Archived threads stay
    # queryable (task evidence) but default inbox views filter them.
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                              server_default="false")
    # Denormalized max(gmail_internal_date) of non-deleted messages. Maintained
    # by inbox_repo.recompute_thread_pointers; kills the recent-message
    # outerjoin sort in list_threads.
    last_activity_at: Mapped[int | None] = mapped_column(BigInteger)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    # Same race-prevention rationale as InboxThread — one message row per (user, gmail_id).
    __table_args__ = (
        UniqueConstraint("user_id", "gmail_id", name="uq_inbox_messages_user_gmail"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(36), ForeignKey("inbox_threads.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    gmail_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # int64 ms since epoch, mirrors Gmail's MessagePart.internalDate
    gmail_internal_date: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    gmail_history_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_addr: Mapped[str | None] = mapped_column(Text)
    from_addr: Mapped[str | None] = mapped_column(Text)
    body_preview: Mapped[str | None] = mapped_column(String(200))
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


class Task(Base):
    """Phase 2A task engine: a user-defined tracker, or (Phase 4) an
    LLM-managed classify-bucket, that threads get linked to and scored
    against. See reference/ for the classify -> link -> extract -> fold
    pipeline this anchors."""
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # null user_id => default bucket shared by all users (Phase 4); always set
    # for tracker-kind tasks.
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="tracker")  # 'tracker' | 'bucket' (Phase 4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")  # formulate_criteria grammar
    # none_as_null=True: a Python None must persist as SQL NULL (not the JSON
    # scalar 'null') so repo.list_active_trackers's `state_schema IS NOT NULL`
    # filter actually distinguishes "no schema yet" from a real schema dict.
    state_schema: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )  # EPS; null = classify-only
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | paused
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)  # SSE gap detection (D4)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskThreadLink(Base):
    """Attaches an inbox thread to a task. LLM-origin links can be detached
    by a later reclassify; user-origin links are sticky (never auto-detached)."""
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
    """One tracked entity within a task's state (e.g. 'stripe' inside a
    'vendor renewals' tracker); '_self' is the singleton entity key for
    single-entity tasks. state is always re-derivable as a fold over this
    entity's applied task_events — refold_entity() rebuilds it after a
    revert/reject."""
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
    """Append-only audit log of every state change to a task's entities —
    the source of truth that TaskStateEntity.state is folded from. Soft
    pointers (entity_id/thread_id/message_id) let events outlive entity
    merges and Gmail-side deletions without losing provenance."""
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
    # Set only on pending_review events, recording which of the validator's
    # guard clauses (transitions.py steps 2/3/5/8) forced the deferral —
    # vocabulary: near_duplicate_entity | backward_move | terminal_locked |
    # fence_blocked | low_confidence. The FIRST guard that forces pending
    # wins; None for applied/rejected/reverted events.
    pending_reason: Mapped[str | None] = mapped_column(String(32))
    # Only set alongside pending_reason='near_duplicate_entity' — the LLM's
    # verbatim entity string (not the normalized key), so the review tray can
    # render "LLM said 'Stripewise Corp', closest match 'stripe'".
    proposed_entity: Mapped[str | None] = mapped_column(String(255))


class Job(Base):
    """Phase 4.5 jobs surface: a persisted, pollable progress row for the
    creation wizard's goal -> draft -> backfill flow and for bucket-delete
    re-triage. Replaces the old fire-and-forget SSE popup (task_draft_ready),
    whose stranded-after-a-connection-blip failure mode motivated moving this
    state into Postgres — see task_engine/jobs_repo.py for the stage machines
    this backs."""
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Jobs are always user-owned — unlike Task.user_id, never NULL.
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # 'creation' | 'delete_retriage'
    task_kind: Mapped[str | None] = mapped_column(String(16))  # 'tracker' | 'bucket' — creation jobs only
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    # Denormalized: true only while stage='draft_ready' — the header chip's
    # blue-dot query reads this instead of re-deriving it from stage.
    needs_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # The proposed draft (creation jobs): {"proposal": {name, description,
    # state_schema, keyword_probes}, "positives": [...], "near_misses": [...]}.
    # Criteria is only formulated at confirm time from description+examples.
    # none_as_null=True for the same reason as Task.state_schema — a Python None
    # must persist as SQL NULL, not the JSON scalar 'null'.
    payload: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )
    # Set at confirm-time (creation) or at enqueue-time (delete_retriage = the
    # deleted bucket's id).
    task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tasks.id"))
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")  # creation jobs; display-name fallback
    scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)  # populated on stage='failed'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # user dismissal
