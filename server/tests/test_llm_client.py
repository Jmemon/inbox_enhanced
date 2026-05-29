import pytest
from app.llm import client


@pytest.fixture(autouse=True)
def _reset():
    client.reset_for_tests(); yield; client.reset_for_tests()


def test_call_messages_returns_empty_on_error():
    """Per-call exceptions must not propagate. classify_batch depends on
    per-thread failures degrading to no-fit, not crashing the gather."""
    client._ensure_initialized()
    # Mimic the OpenAI client shape: client.chat.completions.create(...)
    class _Boom:
        async def create(self, **kw): raise RuntimeError("nope")
    class _Chat: completions = _Boom()
    class _C: chat = _Chat()
    client._state["client"] = _C()
    assert client.run_in_loop(client.call_messages(model="m", system="s", user="u")) == ""
