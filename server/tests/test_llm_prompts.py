from datetime import datetime, timezone
from app.db.models import Task
from app.llm.prompts import classify_thread, score_thread


def _b(id_, name):
    return Task(id=id_, user_id=None, kind="bucket", name=name, goal="", criteria="x",
               state_schema=None, status="active", version=1, is_deleted=False,
               created_at=datetime.now(timezone.utc))


def test_classify_user_message_includes_buckets_and_stability_hint_when_set():
    msg = classify_thread.build_user_message(
        thread_str="BODY", buckets=[_b("b1", "Important")], current_bucket_name="Important",
    )
    assert "Important" in msg and "BODY" in msg and "currently classified" in msg


def test_classify_parse_handles_valid_null_garbage_fenced_and_collisions():
    bs = [_b("b1", "Important"), _b("b2", "Receipts")]
    assert classify_thread.parse_response('{"bucket_name": "Important"}', bs) == "b1"
    assert classify_thread.parse_response('{"bucket_name": null}', bs) is None
    assert classify_thread.parse_response("not json", bs) is None
    assert classify_thread.parse_response('```json\n{"bucket_name": "Receipts"}\n```', bs) == "b2"
    assert classify_thread.parse_response('{"bucket_name": "Unknown"}', bs) is None
    dup = [_b("b1", "Dup"), _b("b2", "Dup")]
    assert classify_thread.parse_response('{"bucket_name": "Dup"}', dup) is None


def test_score_parse_validates_range():
    assert score_thread.parse_response('{"score": 8, "rationale": "r", "snippet": "s"}') \
        == {"score": 8, "rationale": "r", "snippet": "s"}
    assert score_thread.parse_response('{"score": 99}') is None
