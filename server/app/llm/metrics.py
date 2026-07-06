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
