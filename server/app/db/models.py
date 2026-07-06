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


class Bucket(Base):
    __tablename__ = "buckets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # null user_id => default bucket shared by all users
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Soft-delete flag for custom buckets. Defaults are never deleted (their
    # rows always have is_deleted=False). When True, GET /api/buckets omits
    # the row and the classifier excludes it from _available_bucket_ids.
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


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
    bucket_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("buckets.id"))
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
