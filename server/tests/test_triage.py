"""Task 5: the single triage LLM call that replaces classify — one call per
thread returns both the bucket pick AND tracker relevance (D2: no doubled LLM
volume). Covers triage_thread (prompt + parse discipline mirrors
classify_thread's exactly), classify.triage() (same asyncio.gather shape as
classify(), stage="classify"), and gmail_sync._triage_batch (the zero-tracker
regression guarantee: bucket wiring must match the old _classify_batch path)."""

import pytest
from app.db.models import Bucket, Task
from app.gmail.parser import ParsedMessage, ParsedThread
from app.llm import classify as classify_mod, client as llm_client
from app.llm.prompts import triage_thread


def _t(tid="gT1"):
    m = ParsedMessage(gmail_message_id=f"m_{tid}", gmail_thread_id=tid,
                      gmail_internal_date=1, gmail_history_id="1",
                      subject="s", from_addr="a@b", to_addr="me",
                      body_text="b", body_preview="b")
    return ParsedThread(gmail_thread_id=tid, subject="s", recent_internal_date=1, messages=[m])


def _b(id_, name):
    return Bucket(id=id_, user_id=None, name=name, criteria="x", is_deleted=False)


def _task(id_, name, criteria="tracks x"):
    return Task(id=id_, user_id=None, kind="tracker", name=name, goal="", criteria=criteria,
                state_schema=None, status="active", version=1, is_deleted=False)


@pytest.fixture(autouse=True)
def _reset():
    llm_client.reset_for_tests()
    yield
    llm_client.reset_for_tests()


# ---------------------------------------------------------------------------
# triage_thread.build_user_message
# ---------------------------------------------------------------------------


def test_build_user_message_matches_classify_thread_when_no_trackers():
    """With zero trackers, the rendered message must be byte-identical to
    classify_thread's — this is the substrate of the zero-tracker regression
    guarantee: same prompt in, same behavior out."""
    from app.llm.prompts import classify_thread
    buckets = [_b("b1", "Important")]
    classify_msg = classify_thread.build_user_message(
        thread_str="BODY", buckets=buckets, current_bucket_name="Important",
    )
    triage_msg = triage_thread.build_user_message(
        thread_str="BODY", buckets=buckets, trackers=[], current_bucket_name="Important",
    )
    assert triage_msg == classify_msg


def test_build_user_message_appends_task_blocks_for_trackers():
    buckets = [_b("b1", "Important")]
    trackers = [_task("tk1", "Visa Renewal", criteria="tracks visa renewal status")]
    msg = triage_thread.build_user_message(
        thread_str="BODY", buckets=buckets, trackers=trackers, current_bucket_name=None,
    )
    assert 'Important' in msg and 'BODY' in msg
    assert '<task name="Visa Renewal">' in msg
    assert "tracks visa renewal status" in msg


# ---------------------------------------------------------------------------
# triage_thread.parse_response
# ---------------------------------------------------------------------------


def test_parse_response_happy_path_bucket_and_task():
    buckets = [_b("b1", "Important")]
    trackers = [_task("tk1", "Visa Renewal")]
    text = '{"bucket_name": "Important", "relevant_tasks": [{"name": "Visa Renewal", "confidence": 87}]}'
    bucket_id, tasks = triage_thread.parse_response(text, buckets, trackers)
    assert bucket_id == "b1"
    assert tasks == [("tk1", 87)]


def test_parse_response_null_bucket_still_resolves_tasks():
    buckets = [_b("b1", "Important")]
    trackers = [_task("tk1", "Visa Renewal")]
    text = '{"bucket_name": null, "relevant_tasks": [{"name": "Visa Renewal", "confidence": 50}]}'
    bucket_id, tasks = triage_thread.parse_response(text, buckets, trackers)
    assert bucket_id is None
    assert tasks == [("tk1", 50)]


def test_parse_response_unknown_task_name_dropped():
    buckets = [_b("b1", "Important")]
    trackers = [_task("tk1", "Visa Renewal")]
    text = '{"bucket_name": "Important", "relevant_tasks": [{"name": "Ghost Task", "confidence": 90}]}'
    bucket_id, tasks = triage_thread.parse_response(text, buckets, trackers)
    assert bucket_id == "b1"
    assert tasks == []


def test_parse_response_duplicate_task_name_ambiguous_dropped():
    buckets = [_b("b1", "Important")]
    trackers = [_task("tk1", "Dup"), _task("tk2", "Dup")]
    text = '{"bucket_name": null, "relevant_tasks": [{"name": "Dup", "confidence": 90}]}'
    bucket_id, tasks = triage_thread.parse_response(text, buckets, trackers)
    assert tasks == []


def test_parse_response_unknown_bucket_name_dropped():
    buckets = [_b("b1", "Important")]
    text = '{"bucket_name": "Unknown", "relevant_tasks": []}'
    bucket_id, tasks = triage_thread.parse_response(text, buckets, [])
    assert bucket_id is None


def test_parse_response_malformed_json_returns_none_empty():
    buckets = [_b("b1", "Important")]
    trackers = [_task("tk1", "Visa Renewal")]
    assert triage_thread.parse_response("not json", buckets, trackers) == (None, [])
    assert triage_thread.parse_response("", buckets, trackers) == (None, [])


def test_parse_response_fenced_json_supported():
    buckets = [_b("b1", "Important")]
    text = '```json\n{"bucket_name": "Important", "relevant_tasks": []}\n```'
    bucket_id, tasks = triage_thread.parse_response(text, buckets, [])
    assert bucket_id == "b1"
    assert tasks == []


def test_parse_response_confidence_clamped_to_0_100():
    trackers = [_task("tk1", "A"), _task("tk2", "B")]
    text = ('{"bucket_name": null, "relevant_tasks": '
            '[{"name": "A", "confidence": 150}, {"name": "B", "confidence": -30}]}')
    bucket_id, tasks = triage_thread.parse_response(text, [], trackers)
    assert dict(tasks) == {"tk1": 100, "tk2": 0}


def test_parse_response_multi_label_multiple_tasks_relevant():
    trackers = [_task("tk1", "A"), _task("tk2", "B")]
    text = ('{"bucket_name": null, "relevant_tasks": '
            '[{"name": "A", "confidence": 60}, {"name": "B", "confidence": 70}]}')
    bucket_id, tasks = triage_thread.parse_response(text, [], trackers)
    assert dict(tasks) == {"tk1": 60, "tk2": 70}


# ---------------------------------------------------------------------------
# classify.triage()
# ---------------------------------------------------------------------------


def test_triage_preserves_order_and_handles_no_fit_stability(monkeypatch):
    answers = iter([
        '{"bucket_name": "X", "relevant_tasks": [{"name": "Tk", "confidence": 80}]}',
        '{"bucket_name": null, "relevant_tasks": []}',
        '{"bucket_name": "GHOST", "relevant_tasks": []}',
    ])
    seen_stages = []
    seen_models = []

    async def _fake(**kw):
        seen_stages.append(kw.get("stage"))
        seen_models.append(kw.get("model"))
        return next(answers)
    monkeypatch.setattr(llm_client, "call_messages", _fake)

    out = classify_mod.triage(
        [_t("gT1"), _t("gT2"), _t("gT3")],
        [_b("b1", "X")],
        [_task("tk1", "Tk")],
        [None, "b1", "b1"],
    )
    assert out == [("b1", [("tk1", 80)]), ("b1", []), ("b1", [])]
    assert all(s == "classify" for s in seen_stages)
    assert all(m == "anthropic/claude-haiku-4-5" for m in seen_models)


def test_triage_empty_threads_returns_empty():
    assert classify_mod.triage([], [_b("b1", "X")], [_task("tk1", "Tk")], []) == []


def test_triage_no_buckets_no_trackers_skips_llm_call(monkeypatch):
    called = False

    async def _fake(**kw):
        nonlocal called
        called = True
        return '{"bucket_name": null, "relevant_tasks": []}'
    monkeypatch.setattr(llm_client, "call_messages", _fake)

    out = classify_mod.triage([_t()], [], [], [None])
    assert out == [(None, [])]
    assert called is False


def test_triage_mismatched_current_bucket_ids_length_raises():
    with pytest.raises(ValueError):
        classify_mod.triage([_t()], [_b("b1", "X")], [], [])


def test_triage_zero_trackers_matches_classify_bucket_behavior(monkeypatch):
    """Regression guarantee substrate: with zero trackers, triage()'s bucket
    resolution must behave identically to classify() for the same inputs —
    same no-fit fallback to current_bucket_id, same order preservation."""
    answers_classify = iter(['{"bucket_name": "X"}', '{"bucket_name": null}'])
    answers_triage = iter([
        '{"bucket_name": "X", "relevant_tasks": []}',
        '{"bucket_name": null, "relevant_tasks": []}',
    ])

    async def _fake_classify(**kw):
        return next(answers_classify)

    async def _fake_triage(**kw):
        return next(answers_triage)

    threads = [_t("gT1"), _t("gT2")]
    buckets = [_b("b1", "X")]
    current = [None, "b1"]

    monkeypatch.setattr(llm_client, "call_messages", _fake_classify)
    classify_out = classify_mod.classify(threads, buckets, current)

    monkeypatch.setattr(llm_client, "call_messages", _fake_triage)
    triage_out = classify_mod.triage(threads, buckets, [], current)

    assert [b for b, _ in triage_out] == classify_out
