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
