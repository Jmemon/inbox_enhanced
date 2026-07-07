"""Tests for llm/prompts/extract_transition.py: prompt rendering, response
shape-checking, and the per-message-marker thread renderer. Pure — no
database, mirrors test_llm_prompts.py's plain-ORM-object-construction pattern
(no session needed to instantiate a TaskStateEntity)."""

import json
from datetime import datetime, timezone

from app.db.models import TaskStateEntity
from app.gmail.parser import ParsedMessage, ParsedThread
from app.llm.prompts import extract_transition
from app.task_engine.schema import AttributeSpec, EntitySpec, PipelineSpec, TaskStateSchema


def multi_entity_schema() -> TaskStateSchema:
    return TaskStateSchema(
        version=1,
        entity=EntitySpec(
            noun="company",
            identity_hint="the hiring company",
            attributes=[
                AttributeSpec(key="role", type="string"),
                AttributeSpec(key="level", type="enum", values=["junior", "mid", "senior"]),
            ],
        ),
        pipeline=PipelineSpec(stages=["applied", "interview", "onsite"], terminal=["offer", "rejected"]),
    )


def singleton_schema() -> TaskStateSchema:
    return TaskStateSchema(
        version=1, entity=None,
        pipeline=PipelineSpec(stages=["submitted", "in_review"], terminal=["approved", "denied"]),
    )


def _entity(key="stripe", state=None):
    return TaskStateEntity(
        id="e1", task_id="t1", user_id="u1", entity_key=key, display_name=key.title(),
        state=state or {"stage": "interview"}, updated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# build_user_message
# ---------------------------------------------------------------------------


def test_build_user_message_multi_entity_includes_goal_schema_roster_and_thread():
    msg = extract_transition.build_user_message(
        goal="Track my job hunt", schema=multi_entity_schema(), entities=[_entity()],
        thread_str_with_ids="[message gm1 | 2026-01-01T00:00:00+00:00]\nBODY TEXT",
    )
    assert "Track my job hunt" in msg
    # pipeline stages + terminal rendered
    assert "applied" in msg and "interview" in msg and "onsite" in msg
    assert "offer" in msg and "rejected" in msg
    # entity noun + identity hint + attributes (incl. enum values)
    assert "company" in msg and "the hiring company" in msg
    assert "role" in msg and "level" in msg and "junior" in msg and "senior" in msg
    # roster line: "entity_key: {state}"
    assert "stripe" in msg and '"stage": "interview"' in msg
    # thread passed through verbatim, markers intact
    assert "[message gm1 | 2026-01-01T00:00:00+00:00]" in msg and "BODY TEXT" in msg


def test_build_user_message_empty_roster_says_none_yet():
    msg = extract_transition.build_user_message(
        goal="g", schema=multi_entity_schema(), entities=[], thread_str_with_ids="THREAD",
    )
    assert "none yet" in msg


def test_build_user_message_singleton_uses_self_key_no_roster_listing():
    msg = extract_transition.build_user_message(
        goal="Track my visa", schema=singleton_schema(), entities=[], thread_str_with_ids="THREAD",
    )
    assert "_self" in msg
    assert "singleton" in msg
    assert "submitted" in msg and "in_review" in msg
    assert "approved" in msg and "denied" in msg
    assert "THREAD" in msg


# ---------------------------------------------------------------------------
# build_user_message: user_corrections section (spec §4.6 learning loop)
# ---------------------------------------------------------------------------


def test_build_user_message_omits_corrections_section_when_none():
    msg = extract_transition.build_user_message(
        goal="g", schema=singleton_schema(), entities=[], thread_str_with_ids="THREAD",
    )
    assert "Corrections the user has made" not in msg


def test_build_user_message_omits_corrections_section_when_empty_list():
    msg = extract_transition.build_user_message(
        goal="g", schema=singleton_schema(), entities=[], thread_str_with_ids="THREAD",
        user_corrections=[],
    )
    assert "Corrections the user has made" not in msg


def test_build_user_message_with_no_corrections_is_byte_identical_to_pre_task2_shape():
    """Guards against Task 2 accidentally changing the prompt shape for the
    (still-default) no-corrections case."""
    kwargs = dict(
        goal="Track my job hunt", schema=multi_entity_schema(), entities=[_entity()],
        thread_str_with_ids="[message gm1 | 2026-01-01T00:00:00+00:00]\nBODY TEXT",
    )
    without_param = extract_transition.build_user_message(**kwargs)
    with_none = extract_transition.build_user_message(**kwargs, user_corrections=None)
    with_empty = extract_transition.build_user_message(**kwargs, user_corrections=[])
    assert without_param == with_none == with_empty


def test_build_user_message_includes_corrections_section_when_present():
    msg = extract_transition.build_user_message(
        goal="g", schema=singleton_schema(), entities=[], thread_str_with_ids="THREAD",
        user_corrections=[
            'Self: user set stage to "in_review"',
            'Widget Co: user set level to "senior"',
        ],
    )
    assert "Corrections the user has made (respect these):" in msg
    assert '- Self: user set stage to "in_review"' in msg
    assert '- Widget Co: user set level to "senior"' in msg
    # section appears before the thread, after the roster
    assert msg.index("Corrections the user has made") < msg.index("THREAD")


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------


def _valid_item(**overrides) -> dict:
    item = {
        "entity": "stripe", "is_new_entity": False, "field": "stage",
        "new_value": "interview", "evidence_quote": "moving to interview",
        "message_id": "gm2", "confidence": 88,
    }
    item.update(overrides)
    return item


def test_parse_response_valid_array_round_trips():
    item = _valid_item()
    assert extract_transition.parse_response(json.dumps([item])) == [item]


def test_parse_response_empty_array():
    assert extract_transition.parse_response("[]") == []


def test_parse_response_malformed_json_returns_empty():
    assert extract_transition.parse_response("not json at all") == []


def test_parse_response_non_array_json_returns_empty():
    assert extract_transition.parse_response(json.dumps(_valid_item())) == []


def test_parse_response_strips_code_fences():
    text = "```json\n" + json.dumps([_valid_item()]) + "\n```"
    parsed = extract_transition.parse_response(text)
    assert len(parsed) == 1 and parsed[0]["entity"] == "stripe"


def test_parse_response_drops_shape_invalid_items_keeps_valid_ones():
    good = _valid_item()
    missing_keys = {"entity": "ghost"}
    wrong_types = _valid_item(entity="x", is_new_entity="yes")  # not a real bool
    text = json.dumps([good, missing_keys, wrong_types])
    assert extract_transition.parse_response(text) == [good]


def test_parse_response_rejects_out_of_range_confidence():
    assert extract_transition.parse_response(json.dumps([_valid_item(confidence=150)])) == []
    assert extract_transition.parse_response(json.dumps([_valid_item(confidence=-1)])) == []


def test_parse_response_rejects_bool_confidence():
    # bool is an int subclass in Python; True/False must not silently pass as 1/0.
    assert extract_transition.parse_response(json.dumps([_valid_item(confidence=True)])) == []


def test_parse_response_clamps_float_confidence_instead_of_dropping():
    """Parity with triage_thread.parse_response's clamping: a float
    confidence (e.g. a Sonnet '82.5') must round + clamp to an int, not
    discard an otherwise-real transition over a formatting quirk."""
    item = _valid_item(confidence=82.5)
    parsed = extract_transition.parse_response(json.dumps([item]))
    assert len(parsed) == 1
    assert isinstance(parsed[0]["confidence"], int)
    assert parsed[0]["confidence"] == int(round(82.5))
    assert parsed[0]["confidence"] >= 75  # applies under the default task_apply_confidence gate


def test_parse_response_clamps_out_of_range_float_confidence():
    over = extract_transition.parse_response(json.dumps([_valid_item(confidence=150.0)]))
    assert over[0]["confidence"] == 100

    under = extract_transition.parse_response(json.dumps([_valid_item(confidence=-25.0)]))
    assert under[0]["confidence"] == 0


def test_parse_response_rejects_non_string_new_value():
    assert extract_transition.parse_response(json.dumps([_valid_item(new_value=42)])) == []


def test_parse_response_rejects_empty_entity_or_message_id():
    assert extract_transition.parse_response(json.dumps([_valid_item(entity="   ")])) == []
    assert extract_transition.parse_response(json.dumps([_valid_item(message_id="")])) == []


def test_parse_response_ignores_unknown_extra_keys():
    item = _valid_item()
    item["unexpected"] = "noise"
    parsed = extract_transition.parse_response(json.dumps([item]))
    assert len(parsed) == 1
    assert "unexpected" not in parsed[0]


# ---------------------------------------------------------------------------
# thread_to_string_with_ids
# ---------------------------------------------------------------------------


def test_thread_to_string_with_ids_includes_marker_per_message():
    m1 = ParsedMessage(
        gmail_message_id="gm1", gmail_thread_id="gt1", gmail_internal_date=0,
        gmail_history_id="h1", subject="Hi", from_addr="a@x.com", to_addr="me@x.com",
        body_text="Body one", body_preview="Body one",
    )
    m2 = ParsedMessage(
        gmail_message_id="gm2", gmail_thread_id="gt1", gmail_internal_date=86_400_000,
        gmail_history_id="h2", subject="Re: Hi", from_addr="b@x.com", to_addr="me@x.com",
        body_text="Body two", body_preview="Body two",
    )
    thread = ParsedThread(gmail_thread_id="gt1", subject="Hi", recent_internal_date=86_400_000,
                           messages=[m1, m2])
    out = extract_transition.thread_to_string_with_ids(thread)

    assert "[message gm1 | 1970-01-01T00:00:00+00:00]" in out
    assert "[message gm2 | 1970-01-02T00:00:00+00:00]" in out
    assert "Body one" in out and "Body two" in out
    # marker appears before the message it labels
    assert out.index("[message gm1") < out.index("Body one") < out.index("[message gm2")
