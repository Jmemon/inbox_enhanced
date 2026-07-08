"""LLM-backed classifier. One call per thread, parallel via asyncio.gather
under the shared semaphore. Output preserves input order."""

import asyncio
import logging
from app.config import get_settings
from app.db.models import Task
from app.gmail.parser import ParsedThread, thread_to_string
from app.llm import client
from app.llm.prompts import triage_thread

log = logging.getLogger(__name__)


async def _triage_one(
    *, thread: ParsedThread, buckets: list[Task], trackers: list[Task],
    current_bucket_id: str | None, user_id: str | None = None,
    task_id: str | None = None,
) -> tuple[str | None, list[tuple[str, int]]]:
    s = get_settings()
    # Stability hint references the current bucket by name, not opaque id, so
    # the model has semantic context. Falls through to None if current points
    # at a deleted bucket — the LLM can't pick a deleted bucket anyway.
    current_name = next((b.name for b in buckets if b.id == current_bucket_id), None)
    text = await client.call_messages(
        model=s.llm_classify_model,
        system=triage_thread.SYSTEM_PROMPT,
        user=triage_thread.build_user_message(
            thread_str=thread_to_string(thread), buckets=buckets, trackers=trackers,
            current_bucket_name=current_name,
        ),
        # stage="classify" (not "triage"): this call IS the classify call —
        # one llm_calls row per thread, same cost as before triage replaced
        # classify in the sync path (D2 — no doubled LLM volume).
        stage="classify", user_id=user_id, task_id=task_id,
    )
    bucket_id, tasks = triage_thread.parse_response(text, buckets, trackers)
    if bucket_id is None:
        bucket_id = current_bucket_id  # no-fit: keep existing (None for new threads)
    return bucket_id, tasks


def triage(
    threads: list[ParsedThread], buckets: list[Task], trackers: list[Task],
    current_bucket_ids: list[str | None], *, user_id: str | None = None,
    task_id: str | None = None,
) -> list[tuple[str | None, list[tuple[str, int]]]]:
    """The one Haiku call per thread that returns both the bucket pick and
    tracker relevance (D2 — no doubled LLM volume); replaced the old
    classify() on the sync path entirely (classify() itself was deleted in
    Phase 4 Task 3 — triage() is the only classify-stage entrypoint left).
    Same asyncio.gather shape classify() used; output preserves input order.

    task_id is optional and forwarded verbatim to call_messages's own task_id
    kwarg for llm_calls metrics attribution — most callers (the live sync
    path's _triage_batch, _reclassify_all) triage against several trackers at
    once and have no single task_id to attribute a call to, so it defaults to
    None there. backfill_task calls triage() with exactly one tracker in
    `trackers` and passes that tracker's own id, so its LLM spend is
    attributable to the tracker that triggered it.
    """
    if not threads:
        return []
    if len(current_bucket_ids) != len(threads):
        raise ValueError("current_bucket_ids length must match threads length")
    if not buckets and not trackers:
        # Nothing for the LLM to determine either way — skip the call
        # entirely (mirrors the old classify()'s empty-buckets short-circuit).
        return [(None, [])] * len(threads)

    async def _all():
        return await asyncio.gather(*[
            _triage_one(thread=t, buckets=buckets, trackers=trackers,
                       current_bucket_id=cur, user_id=user_id, task_id=task_id)
            for t, cur in zip(threads, current_bucket_ids)
        ])
    return client.run_in_loop(_all())
