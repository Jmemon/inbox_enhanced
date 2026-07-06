"""OpenRouter (OpenAI-compatible) client wrapper.

Owns one AsyncOpenAI per worker process pointed at OpenRouter + one
asyncio.Semaphore(N) bound to a long-lived background event loop, plus a
run_in_loop sync bridge for Celery callers. Lazy-init per fork. call_messages
returns "" on any error so per-thread classify failures degrade to no-fit
instead of crashing a batch.
"""

import asyncio
import logging
import threading
import time
from typing import Any
from openai import AsyncOpenAI
from app.config import get_settings
from app.llm import metrics

log = logging.getLogger(__name__)

_state: dict[str, Any] = {"loop": None, "thread": None, "sem": None, "client": None}
_init_lock = threading.Lock()


def _ensure_initialized() -> None:
    if _state["loop"] is not None:
        return
    with _init_lock:
        if _state["loop"] is not None:
            return
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=_run, name="llm-loop", daemon=True)
        thread.start()
        ready.wait()

        s = get_settings()
        sem = asyncio.run_coroutine_threadsafe(
            _build_semaphore(s.llm_concurrency), loop
        ).result()
        # OpenRouter speaks the OpenAI API; X-Title surfaces this app in the
        # OpenRouter dashboard rankings.
        client = AsyncOpenAI(
            api_key=s.openrouter_api_key,
            base_url=s.openrouter_base_url,
            default_headers={"X-Title": "inbox_enhanced"},
        )
        _state.update(loop=loop, thread=thread, sem=sem, client=client)
        log.info("llm.client: initialized loop + semaphore(n=%d)", s.llm_concurrency)


async def _build_semaphore(n: int) -> asyncio.Semaphore:
    return asyncio.Semaphore(n)


def run_in_loop(coro):
    _ensure_initialized()
    return asyncio.run_coroutine_threadsafe(coro, _state["loop"]).result()


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


def reset_for_tests() -> None:
    if _state["loop"] is not None:
        try:
            _state["loop"].call_soon_threadsafe(_state["loop"].stop)
        except Exception:
            pass
    _state.update(loop=None, thread=None, sem=None, client=None)
