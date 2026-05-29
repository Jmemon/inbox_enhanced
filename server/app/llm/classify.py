"""LLM-backed classifier. One call per thread, parallel via asyncio.gather
under the shared semaphore. Output preserves input order."""

import asyncio
import logging
from app.config import get_settings
from app.db.models import Bucket
from app.gmail.parser import ParsedThread, thread_to_string
from app.llm import client
from app.llm.prompts import classify_thread

log = logging.getLogger(__name__)


async def _classify_one(*, thread: ParsedThread, buckets: list[Bucket], current_bucket_id: str | None) -> str | None:
    s = get_settings()
    # Stability hint references the current bucket by name, not opaque id, so
    # the model has semantic context. Falls through to None if current points
    # at a deleted bucket — the LLM can't pick a deleted bucket anyway.
    current_name = next((b.name for b in buckets if b.id == current_bucket_id), None)
    text = await client.call_messages(
        model=s.llm_classify_model,
        system=classify_thread.SYSTEM_PROMPT,
        user=classify_thread.build_user_message(
            thread_str=thread_to_string(thread), buckets=buckets,
            current_bucket_name=current_name,
        ),
    )
    # parse_response already validates name → id resolution against `buckets`
    # (returns None for null, unknown, or ambiguous duplicate-name picks).
    bid = classify_thread.parse_response(text, buckets)
    if bid is None:
        return current_bucket_id  # no-fit: keep existing (None for new threads)
    return bid


def classify(
    threads: list[ParsedThread], buckets: list[Bucket], current_bucket_ids: list[str | None],
) -> list[str | None]:
    if not threads:
        return []
    if not buckets:
        return [None] * len(threads)
    if len(current_bucket_ids) != len(threads):
        raise ValueError("current_bucket_ids length must match threads length")

    async def _all():
        return await asyncio.gather(*[
            _classify_one(thread=t, buckets=buckets, current_bucket_id=cur)
            for t, cur in zip(threads, current_bucket_ids)
        ])
    return client.run_in_loop(_all())
