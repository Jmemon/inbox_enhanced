"""Task 8: the goal -> proposed task draft flow.

Covers three things:
 - `llm/prompts/propose_task.py`'s pure build_user_message/parse_response
   shape checks (no database, mirrors test_extract_prompt.py's pattern).
 - `task_engine/draft_cache.py`'s pending/ready/load contract (mirrors
   preview_cache's own test conventions, just a different key prefix).
 - `workers/task_engine_tasks.propose_task_draft`'s worker flow: canned LLM
   JSON -> cache-before-publish ordering, the invalid-schema retry-once path
   (asserting the retry's user message contains the first error), the
   double-invalid -> fallback-schema path, and the probes-miss ->
   `tasks._read_candidates` fallback path. Uses eager celery + a file-backed
   sqlite session_factory + fakeredis + a monkeypatched `llm_client.
   call_messages`, matching test_task_engine_tasks.py's conventions exactly.
"""

import os
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, User
from app.inbox import inbox_repo
from app.llm import client as llm_client
from app.llm.prompts import propose_task
from app.task_engine import draft_cache
from app.workers import task_engine_tasks
from app.workers import tasks as tasks_mod

USER_ID = "u1"


# ---------------------------------------------------------------------------
# propose_task.build_user_message
# ---------------------------------------------------------------------------


def test_build_user_message_includes_goal_eps_spec_and_worked_example():
    msg = propose_task.build_user_message(goal="help me get a mortgage")
    assert "help me get a mortgage" in msg
    # compact EPS spec markers
    assert "version" in msg and "pipeline" in msg and "entity" in msg
    assert "stages" in msg and "terminal" in msg
    # one worked example (job hunt), valid enough to prove it's real JSON
    assert "Job hunt" in msg
    example_start = msg.index('{"name": "Job hunt"')
    example_end = msg.index("\n\nGoal:")
    json.loads(msg[example_start:example_end])  # the worked example parses


# ---------------------------------------------------------------------------
# propose_task.parse_response — shape checks only
# ---------------------------------------------------------------------------


def _valid_obj(**overrides) -> dict:
    obj = {
        "name": "Job hunt",
        "description": "Tracks companies I'm interviewing with.",
        "state_schema": {
            "version": 1, "entity": None,
            "pipeline": {"stages": ["applied"], "terminal": ["offer", "rejected"]},
        },
        "keyword_probes": ["interview", "recruiter", "offer"],
    }
    obj.update(overrides)
    return obj


def test_parse_response_valid_object_round_trips():
    obj = _valid_obj()
    parsed = propose_task.parse_response(json.dumps(obj))
    assert parsed == obj


def test_parse_response_truncates_overlong_name_instead_of_dropping():
    long_name = "x" * 60
    parsed = propose_task.parse_response(json.dumps(_valid_obj(name=long_name)))
    assert parsed is not None
    assert parsed["name"] == long_name[:40]
    assert len(parsed["name"]) == 40


def test_parse_response_clamps_extra_keyword_probes_to_eight():
    probes = [f"term{i}" for i in range(12)]
    parsed = propose_task.parse_response(json.dumps(_valid_obj(keyword_probes=probes)))
    assert parsed is not None
    assert parsed["keyword_probes"] == probes[:8]


def test_parse_response_drops_non_string_probes_before_clamping():
    probes = ["good", 42, None, "", "  ", "also good"]
    parsed = propose_task.parse_response(json.dumps(_valid_obj(keyword_probes=probes)))
    assert parsed is not None
    assert parsed["keyword_probes"] == ["good", "also good"]


def test_parse_response_rejects_missing_name():
    obj = _valid_obj()
    del obj["name"]
    assert propose_task.parse_response(json.dumps(obj)) is None


def test_parse_response_rejects_empty_name():
    assert propose_task.parse_response(json.dumps(_valid_obj(name="   "))) is None


def test_parse_response_rejects_non_string_description():
    assert propose_task.parse_response(json.dumps(_valid_obj(description=123))) is None


def test_parse_response_rejects_non_dict_state_schema():
    assert propose_task.parse_response(json.dumps(_valid_obj(state_schema="nope"))) is None
    assert propose_task.parse_response(json.dumps(_valid_obj(state_schema=["a"]))) is None


def test_parse_response_rejects_non_list_keyword_probes():
    assert propose_task.parse_response(json.dumps(_valid_obj(keyword_probes="interview"))) is None


def test_parse_response_malformed_json_returns_none():
    assert propose_task.parse_response("not json at all") is None


def test_parse_response_non_object_json_returns_none():
    assert propose_task.parse_response(json.dumps(["a", "b"])) is None


def test_parse_response_strips_code_fences():
    text = "```json\n" + json.dumps(_valid_obj()) + "\n```"
    parsed = propose_task.parse_response(text)
    assert parsed is not None and parsed["name"] == "Job hunt"


# ---------------------------------------------------------------------------
# draft_cache — mirrors preview_cache's own contract
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr("app.realtime.redis_client.get_redis", lambda: r)
    return r


def test_draft_cache_load_missing_key_returns_none(fake_redis):
    assert draft_cache.load("nope") is None


def test_draft_cache_mark_pending_then_load(fake_redis):
    draft_cache.mark_pending("d1", user_id=USER_ID)
    cached = draft_cache.load("d1")
    assert cached == {"status": "pending", "user_id": USER_ID}


def test_draft_cache_store_result_overwrites_pending_and_spreads_payload(fake_redis):
    draft_cache.mark_pending("d1", user_id=USER_ID)
    draft_cache.store_result("d1", user_id=USER_ID, payload={"proposal": {"name": "X"}, "positives": []})
    cached = draft_cache.load("d1")
    assert cached == {
        "status": "ready", "user_id": USER_ID,
        "proposal": {"name": "X"}, "positives": [],
    }


# ---------------------------------------------------------------------------
# propose_task_draft worker — fixtures + seeding helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture(autouse=True)
def _wire_session_local(session_factory, monkeypatch):
    monkeypatch.setattr("app.workers.task_engine_tasks.SessionLocal", session_factory)


@pytest.fixture(autouse=True)
def _reset_llm_loop():
    llm_client.reset_for_tests()
    yield
    llm_client.reset_for_tests()


def _seed_user(session_factory, user_id=USER_ID):
    db = session_factory()
    db.add(User(id=user_id, email=f"{user_id}@x.com", created_at=datetime.now(timezone.utc)))
    db.commit()
    db.close()


def _seed_thread(
    session_factory, *, user_id=USER_ID, gmail_thread_id, subject, body,
    from_addr="a@b.co", date=100,
):
    db = session_factory()
    inbox_repo.upsert_thread(db, user_id=user_id, gmail_thread_id=gmail_thread_id,
                             subject=subject, bucket_id=None)
    inbox_repo.upsert_message(
        db, user_id=user_id, gmail_thread_id=gmail_thread_id,
        gmail_message_id=f"m-{gmail_thread_id}", gmail_internal_date=date,
        gmail_history_id="h1", to_addr="me@x.com", from_addr=from_addr,
        body_preview=body[:150], body_text=body,
    )
    db.commit()
    db.close()


def _proposal_json(*, name="Job hunt", description="Tracks companies I'm interviewing with.",
                   state_schema=None, keyword_probes=None) -> str:
    schema = state_schema if state_schema is not None else {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
    }
    probes = keyword_probes if keyword_probes is not None else ["interview", "recruiter"]
    return json.dumps({
        "name": name, "description": description,
        "state_schema": schema, "keyword_probes": probes,
    })


def _fake_call_messages(propose_responses: list[str], score_response: str =
                        '{"score": 9, "rationale": "match", "snippet": "hit"}'):
    """stage="propose" calls pop responses off `propose_responses` in order
    (supports the retry-once flow); stage="score" calls (from
    tasks._score_all) always return `score_response`. Records every call's
    kwargs on `.calls` so tests can inspect model/user/stage."""
    remaining = list(propose_responses)
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stage") == "propose":
            return remaining.pop(0)
        return score_response

    _fake.calls = calls
    return _fake


# ---------------------------------------------------------------------------
# Happy path: canned JSON -> cache-before-publish, positives populated
# ---------------------------------------------------------------------------


def test_propose_task_draft_happy_path_caches_before_publishing(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed for Monday",
                from_addr="recruiter@acme.co")
    _seed_thread(session_factory, gmail_thread_id="g2", subject="Grocery receipt",
                body="Thanks for your order of milk and eggs",
                from_addr="store@example.com")

    order: list[str] = []
    orig_store = draft_cache.store_result

    def _record_store(*a, **kw):
        order.append("cache")
        return orig_store(*a, **kw)

    monkeypatch.setattr(draft_cache, "store_result", _record_store)

    published: list[tuple] = []

    def _record_publish(user_id, event, payload):
        order.append("publish")
        published.append((user_id, event, payload))

    monkeypatch.setattr(tasks_mod, "_publish", _record_publish)

    fake = _fake_call_messages(
        [_proposal_json(keyword_probes=["interview"])],
        score_response='{"score": 9, "rationale": "match", "snippet": "onsite interview"}',
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, "draft-1", "help me land a new job"])

    # cache-write-before-publish ordering, asserted via the recording fakes above.
    assert order == ["cache", "publish"]
    assert len(published) == 1
    user_id, event, payload = published[0]
    assert user_id == USER_ID
    assert event == "task_draft_ready"
    assert payload == {"draft_id": "draft-1"}

    settings = get_settings()
    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 1
    assert propose_calls[0]["model"] == settings.llm_extract_model  # resolved, not the literal
    assert propose_calls[0]["user_id"] == USER_ID
    assert "task_id" not in propose_calls[0] or propose_calls[0]["task_id"] is None

    score_calls = [c for c in fake.calls if c.get("stage") == "score"]
    assert score_calls and score_calls[0]["model"] == settings.llm_classify_model

    cached = draft_cache.load("draft-1")
    assert cached["status"] == "ready"
    assert cached["user_id"] == USER_ID
    proposal = cached["proposal"]
    assert proposal["name"] == "Job hunt"
    assert proposal["keyword_probes"] == ["interview"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
    }
    # only the "interview" thread matched the probe -> only it was scored
    assert len(cached["positives"]) == 1
    assert cached["positives"][0]["thread_id"]
    assert cached["near_misses"] == []


# ---------------------------------------------------------------------------
# Invalid schema -> retry once with the error appended -> succeeds
# ---------------------------------------------------------------------------


def test_propose_task_draft_retries_once_on_invalid_schema_then_succeeds(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed", from_addr="recruiter@acme.co")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)

    invalid_schema = {"version": 1, "entity": None,
                      "pipeline": {"stages": [], "terminal": ["done"]}}
    bad = _proposal_json(state_schema=invalid_schema, keyword_probes=["interview"])
    good = _proposal_json(keyword_probes=["interview"])

    fake = _fake_call_messages([bad, good])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, "draft-2", "help me land a new job"])

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2
    # the retry's user message carries the first attempt's validator error
    assert "pipeline must declare at least one stage" in propose_calls[1]["user"]

    cached = draft_cache.load("draft-2")
    assert cached["proposal"]["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
    }


# ---------------------------------------------------------------------------
# Double-invalid schema -> fallback schema; rest of the proposal survives
# ---------------------------------------------------------------------------


def test_propose_task_draft_falls_back_to_default_schema_after_second_invalid_attempt(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed", from_addr="recruiter@acme.co")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)

    invalid_schema_1 = {"version": 1, "entity": None,
                        "pipeline": {"stages": [], "terminal": ["done"]}}
    invalid_schema_2 = {"version": 1, "entity": None,
                        "pipeline": {"stages": ["a", "a"], "terminal": ["done"]}}
    bad1 = _proposal_json(state_schema=invalid_schema_1, keyword_probes=["interview"])
    bad2 = _proposal_json(name="Job hunt (retry)", state_schema=invalid_schema_2,
                          keyword_probes=["interview"])

    fake = _fake_call_messages([bad1, bad2])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, "draft-3", "help me land a new job"])

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2  # no third attempt -- fallback kicks in immediately

    cached = draft_cache.load("draft-3")
    proposal = cached["proposal"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["in_progress"], "terminal": ["done"]},
    }
    # name/description/probes still come from the retry's (also schema-invalid) response
    assert proposal["name"] == "Job hunt (retry)"
    assert proposal["keyword_probes"] == ["interview"]


# ---------------------------------------------------------------------------
# Unparseable first response -> retry once (symmetry with the schema-invalid
# retry above). propose_task_draft spends exactly one retry total per draft,
# regardless of which of the two failure shapes (unparseable vs.
# schema-invalid) fires first.
# ---------------------------------------------------------------------------


def test_propose_task_draft_retries_once_on_unparseable_first_response_then_succeeds(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed", from_addr="recruiter@acme.co")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)

    # "" mirrors call_messages' own degrade-on-error behavior (client.py:
    # "call_messages returns \"\" on any error") -- a transient API failure,
    # not a schema problem, so parse_response("") returns None outright.
    good = _proposal_json(keyword_probes=["interview"])
    fake = _fake_call_messages(["", good])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, "draft-5", "help me land a new job"])

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2
    # the retry's user message carries the generic re-ask nudge, not a
    # validator error -- there was nothing to validate, the first response
    # never parsed at all.
    assert "valid JSON" in propose_calls[1]["user"]

    cached = draft_cache.load("draft-5")
    proposal = cached["proposal"]
    assert proposal["name"] == "Job hunt"
    assert proposal["keyword_probes"] == ["interview"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
    }
    # full-quality draft: the fallback schema was never needed, and scoring
    # ran against the real (retry) proposal's name/description.
    assert len(cached["positives"]) == 1


def test_propose_task_draft_falls_back_when_both_attempts_unparseable(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)

    fake = _fake_call_messages(["", ""])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(
        args=[USER_ID, "draft-6", "help me plan a wedding"],
    )

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2  # no third attempt

    cached = draft_cache.load("draft-6")
    proposal = cached["proposal"]
    # synthetic draft carved from the goal itself
    assert proposal["name"] == "help me plan a wedding"[:40]
    assert proposal["description"] == "help me plan a wedding"
    assert proposal["keyword_probes"] == []
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["in_progress"], "terminal": ["done"]},
    }


def test_propose_task_draft_falls_back_when_unparseable_then_schema_invalid(
    session_factory, fake_redis, monkeypatch,
):
    """Mixed failure shapes: first attempt unparseable (spends the draft's
    one retry), retry attempt parses but has an invalid schema. Must NOT
    spend a third LLM call -- the retry budget was already used on the
    first attempt's unparseable response, so a schema-invalid retry
    response goes straight to the fallback schema instead of triggering its
    own second retry."""
    _seed_user(session_factory)
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)

    invalid_schema = {"version": 1, "entity": None,
                      "pipeline": {"stages": [], "terminal": ["done"]}}
    bad = _proposal_json(name="Recovered name", state_schema=invalid_schema,
                         keyword_probes=["interview"])
    fake = _fake_call_messages(["", bad])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(
        args=[USER_ID, "draft-7", "help me land a new job"],
    )

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2  # no third attempt

    cached = draft_cache.load("draft-7")
    proposal = cached["proposal"]
    # name/description/probes still come from the retry's parseable (if
    # schema-invalid) response -- only the schema itself falls back.
    assert proposal["name"] == "Recovered name"
    assert proposal["keyword_probes"] == ["interview"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["in_progress"], "terminal": ["done"]},
    }


# ---------------------------------------------------------------------------
# Unknown user -> skip entirely, no LLM spend (parity with draft_preview_bucket)
# ---------------------------------------------------------------------------


def test_propose_task_draft_skips_for_unknown_user(session_factory, fake_redis, monkeypatch):
    # deliberately never seed a User row
    fake = _fake_call_messages([_proposal_json()])
    monkeypatch.setattr(llm_client, "call_messages", fake)
    published: list[bool] = []
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: published.append(True))

    task_engine_tasks.propose_task_draft.apply(args=["no-such-user", "draft-8", "some goal"])

    assert fake.calls == []  # no LLM spend for a bogus user
    assert published == []
    assert draft_cache.load("draft-8") is None


# ---------------------------------------------------------------------------
# Probes miss everything -> falls back to tasks._read_candidates
# ---------------------------------------------------------------------------


def test_propose_task_draft_falls_back_to_read_candidates_when_probes_find_nothing(
    session_factory, fake_redis, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="onsite interview confirmed", from_addr="recruiter@acme.co")
    _seed_thread(session_factory, gmail_thread_id="g2", subject="Grocery receipt",
                body="milk and eggs", from_addr="store@example.com")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)

    fake = _fake_call_messages(
        [_proposal_json(keyword_probes=["zzznomatchterm"])],
        score_response='{"score": 8, "rationale": "ok", "snippet": "x"}',
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)

    real_read_candidates = tasks_mod._read_candidates
    spy = MagicMock(side_effect=real_read_candidates)
    monkeypatch.setattr(tasks_mod, "_read_candidates", spy)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, "draft-4", "some goal"])

    spy.assert_called_once()
    assert spy.call_args.kwargs == {
        "user_id": USER_ID, "exclude": set(),
        "limit": task_engine_tasks.PROPOSE_READ_CANDIDATES_LIMIT,
    }

    cached = draft_cache.load("draft-4")
    # both seeded threads came back through the recency-pool fallback and scored
    assert len(cached["positives"]) == 2
