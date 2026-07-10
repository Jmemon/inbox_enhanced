"""Task 8 (+ Phase 4.5 Task 3 rework): the goal -> proposed task draft flow.

Covers two things:
 - `llm/prompts/propose_task.py`'s pure build_user_message/parse_response
   shape checks (no database, mirrors test_extract_prompt.py's pattern).
 - `workers/task_engine_tasks.propose_task_draft`'s worker flow: canned LLM
   JSON -> commit-before-publish ordering into the job row's `payload`/
   `stage` (Phase 4.5 Task 3 -- replaces the retired Redis draft_cache), the
   invalid-schema retry-once path (asserting the retry's user message
   contains the first error), the double-invalid -> fallback-schema path,
   the probes-miss -> `tasks._read_candidates` fallback path, the unknown-
   user/unknown-job skip-without-LLM-spend paths, and the new top-level
   exception -> `mark_failed` + re-raise guard. Uses eager celery + a
   file-backed sqlite session_factory + a monkeypatched `llm_client.
   call_messages`, matching test_task_engine_tasks.py's conventions exactly
   (job seeding via `jobs_repo` instead of the retired draft_cache/fakeredis).
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
from app.db.models import Base, Job, User
from app.inbox import inbox_repo
from app.llm import client as llm_client
from app.llm.prompts import propose_task
from app.task_engine import jobs_repo
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


def _seed_job(session_factory, *, user_id=USER_ID, task_kind="tracker", goal="goal text") -> str:
    db = session_factory()
    job = jobs_repo.create_job(db, user_id=user_id, kind="creation", task_kind=task_kind, goal=goal)
    db.commit()
    job_id = job.id
    db.close()
    return job_id


def _get_job(session_factory, job_id, user_id=USER_ID):
    db = session_factory()
    job = jobs_repo.get_owned_job(db, user_id=user_id, job_id=job_id)
    db.close()
    return job


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
# Happy path: canned JSON -> commit-before-publish, positives populated
# ---------------------------------------------------------------------------


def test_propose_task_draft_happy_path_commits_before_publishing(
    session_factory, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed for Monday",
                from_addr="recruiter@acme.co")
    _seed_thread(session_factory, gmail_thread_id="g2", subject="Grocery receipt",
                body="Thanks for your order of milk and eggs",
                from_addr="store@example.com")
    job_id = _seed_job(session_factory, goal="help me land a new job")

    published: list[tuple] = []

    def _record_publish(user_id, event, payload):
        # commit-before-publish: a FRESH session (separate sqlite connection)
        # must already see the job row in its post-write state by the time
        # this fires, or the write hadn't actually committed yet.
        row = _get_job(session_factory, job_id)
        assert row.stage == "draft_ready"
        assert row.payload is not None
        published.append((user_id, event, payload))

    monkeypatch.setattr(tasks_mod, "_publish", _record_publish)

    fake = _fake_call_messages(
        [_proposal_json(keyword_probes=["interview"])],
        score_response='{"score": 9, "rationale": "match", "snippet": "onsite interview"}',
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, job_id, "help me land a new job"])

    assert len(published) == 1
    user_id, event, payload = published[0]
    assert user_id == USER_ID
    assert event == "job_updated"
    assert payload == {"job_id": job_id}

    settings = get_settings()
    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 1
    assert propose_calls[0]["model"] == settings.llm_extract_model  # resolved, not the literal
    assert propose_calls[0]["user_id"] == USER_ID
    assert "task_id" not in propose_calls[0] or propose_calls[0]["task_id"] is None

    score_calls = [c for c in fake.calls if c.get("stage") == "score"]
    assert score_calls and score_calls[0]["model"] == settings.llm_classify_model

    row = _get_job(session_factory, job_id)
    assert row.stage == "draft_ready"
    assert row.needs_user is True
    proposal = row.payload["proposal"]
    assert proposal["name"] == "Job hunt"
    assert proposal["keyword_probes"] == ["interview"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
    }
    # only the "interview" thread matched the probe -> only it was scored
    assert len(row.payload["positives"]) == 1
    assert row.payload["positives"][0]["thread_id"]
    assert row.payload["near_misses"] == []


# ---------------------------------------------------------------------------
# Invalid schema -> retry once with the error appended -> succeeds
# ---------------------------------------------------------------------------


def test_propose_task_draft_retries_once_on_invalid_schema_then_succeeds(
    session_factory, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed", from_addr="recruiter@acme.co")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)
    job_id = _seed_job(session_factory, goal="help me land a new job")

    invalid_schema = {"version": 1, "entity": None,
                      "pipeline": {"stages": [], "terminal": ["done"]}}
    bad = _proposal_json(state_schema=invalid_schema, keyword_probes=["interview"])
    good = _proposal_json(keyword_probes=["interview"])

    fake = _fake_call_messages([bad, good])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, job_id, "help me land a new job"])

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2
    # the retry's user message carries the first attempt's validator error
    assert "pipeline must declare at least one stage" in propose_calls[1]["user"]

    row = _get_job(session_factory, job_id)
    assert row.payload["proposal"]["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
    }


# ---------------------------------------------------------------------------
# Double-invalid schema -> fallback schema; rest of the proposal survives
# ---------------------------------------------------------------------------


def test_propose_task_draft_falls_back_to_default_schema_after_second_invalid_attempt(
    session_factory, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed", from_addr="recruiter@acme.co")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)
    job_id = _seed_job(session_factory, goal="help me land a new job")

    invalid_schema_1 = {"version": 1, "entity": None,
                        "pipeline": {"stages": [], "terminal": ["done"]}}
    invalid_schema_2 = {"version": 1, "entity": None,
                        "pipeline": {"stages": ["a", "a"], "terminal": ["done"]}}
    bad1 = _proposal_json(state_schema=invalid_schema_1, keyword_probes=["interview"])
    bad2 = _proposal_json(name="Job hunt (retry)", state_schema=invalid_schema_2,
                          keyword_probes=["interview"])

    fake = _fake_call_messages([bad1, bad2])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, job_id, "help me land a new job"])

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2  # no third attempt -- fallback kicks in immediately

    row = _get_job(session_factory, job_id)
    proposal = row.payload["proposal"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["in_progress"], "terminal": ["done"]},
    }
    # name/description/probes still come from the retry's (also schema-invalid) response
    assert proposal["name"] == "Job hunt (retry)"
    assert proposal["keyword_probes"] == ["interview"]


# ---------------------------------------------------------------------------
# Unparseable first response -> retry once (symmetry with the schema-invalid
# retry above). propose_task_draft spends exactly one retry total per job,
# regardless of which of the two failure shapes (unparseable vs.
# schema-invalid) fires first.
# ---------------------------------------------------------------------------


def test_propose_task_draft_retries_once_on_unparseable_first_response_then_succeeds(
    session_factory, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="Your onsite interview is confirmed", from_addr="recruiter@acme.co")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)
    job_id = _seed_job(session_factory, goal="help me land a new job")

    # "" mirrors call_messages' own degrade-on-error behavior (client.py:
    # "call_messages returns \"\" on any error") -- a transient API failure,
    # not a schema problem, so parse_response("") returns None outright.
    good = _proposal_json(keyword_probes=["interview"])
    fake = _fake_call_messages(["", good])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, job_id, "help me land a new job"])

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2
    # the retry's user message carries the generic re-ask nudge, not a
    # validator error -- there was nothing to validate, the first response
    # never parsed at all.
    assert "valid JSON" in propose_calls[1]["user"]

    row = _get_job(session_factory, job_id)
    proposal = row.payload["proposal"]
    assert proposal["name"] == "Job hunt"
    assert proposal["keyword_probes"] == ["interview"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["applied", "interview"], "terminal": ["offer", "rejected"]},
    }
    # full-quality draft: the fallback schema was never needed, and scoring
    # ran against the real (retry) proposal's name/description.
    assert len(row.payload["positives"]) == 1


def test_propose_task_draft_falls_back_when_both_attempts_unparseable(
    session_factory, monkeypatch,
):
    _seed_user(session_factory)
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)
    job_id = _seed_job(session_factory, goal="help me plan a wedding")

    fake = _fake_call_messages(["", ""])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(
        args=[USER_ID, job_id, "help me plan a wedding"],
    )

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2  # no third attempt

    row = _get_job(session_factory, job_id)
    proposal = row.payload["proposal"]
    # synthetic draft carved from the goal itself -- this is a degraded but
    # still-successful draft_ready outcome, NOT a job failure.
    assert row.stage == "draft_ready"
    assert proposal["name"] == "help me plan a wedding"[:40]
    assert proposal["description"] == "help me plan a wedding"
    assert proposal["keyword_probes"] == []
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["in_progress"], "terminal": ["done"]},
    }


def test_propose_task_draft_falls_back_when_unparseable_then_schema_invalid(
    session_factory, monkeypatch,
):
    """Mixed failure shapes: first attempt unparseable (spends the job's one
    retry), retry attempt parses but has an invalid schema. Must NOT spend a
    third LLM call -- the retry budget was already used on the first
    attempt's unparseable response, so a schema-invalid retry response goes
    straight to the fallback schema instead of triggering its own second
    retry."""
    _seed_user(session_factory)
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)
    job_id = _seed_job(session_factory, goal="help me land a new job")

    invalid_schema = {"version": 1, "entity": None,
                      "pipeline": {"stages": [], "terminal": ["done"]}}
    bad = _proposal_json(name="Recovered name", state_schema=invalid_schema,
                         keyword_probes=["interview"])
    fake = _fake_call_messages(["", bad])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    task_engine_tasks.propose_task_draft.apply(
        args=[USER_ID, job_id, "help me land a new job"],
    )

    propose_calls = [c for c in fake.calls if c.get("stage") == "propose"]
    assert len(propose_calls) == 2  # no third attempt

    row = _get_job(session_factory, job_id)
    proposal = row.payload["proposal"]
    # name/description/probes still come from the retry's parseable (if
    # schema-invalid) response -- only the schema itself falls back.
    assert proposal["name"] == "Recovered name"
    assert proposal["keyword_probes"] == ["interview"]
    assert proposal["state_schema"] == {
        "version": 1, "entity": None,
        "pipeline": {"stages": ["in_progress"], "terminal": ["done"]},
    }


# ---------------------------------------------------------------------------
# Unknown user / unknown job -> skip entirely, no LLM spend (parity with
# draft_preview_bucket)
# ---------------------------------------------------------------------------


def test_propose_task_draft_skips_for_unknown_user(session_factory, monkeypatch):
    # deliberately never seed a User row
    fake = _fake_call_messages([_proposal_json()])
    monkeypatch.setattr(llm_client, "call_messages", fake)
    published: list[bool] = []
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: published.append(True))

    task_engine_tasks.propose_task_draft.apply(args=["no-such-user", "no-such-job", "some goal"])

    assert fake.calls == []  # no LLM spend for a bogus user
    assert published == []


def test_propose_task_draft_skips_for_unknown_job(session_factory, monkeypatch):
    # user exists, but the job_id doesn't -- e.g. dismissed-and-since-purged,
    # or simply a bad id. Must not spend an LLM call either.
    _seed_user(session_factory)
    fake = _fake_call_messages([_proposal_json()])
    monkeypatch.setattr(llm_client, "call_messages", fake)
    published: list[bool] = []
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: published.append(True))

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, "no-such-job", "some goal"])

    assert fake.calls == []
    assert published == []


# ---------------------------------------------------------------------------
# Probes miss everything -> falls back to tasks._read_candidates
# ---------------------------------------------------------------------------


def test_propose_task_draft_falls_back_to_read_candidates_when_probes_find_nothing(
    session_factory, monkeypatch,
):
    _seed_user(session_factory)
    _seed_thread(session_factory, gmail_thread_id="g1", subject="Interview scheduled",
                body="onsite interview confirmed", from_addr="recruiter@acme.co")
    _seed_thread(session_factory, gmail_thread_id="g2", subject="Grocery receipt",
                body="milk and eggs", from_addr="store@example.com")
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)
    job_id = _seed_job(session_factory, goal="some goal")

    fake = _fake_call_messages(
        [_proposal_json(keyword_probes=["zzznomatchterm"])],
        score_response='{"score": 8, "rationale": "ok", "snippet": "x"}',
    )
    monkeypatch.setattr(llm_client, "call_messages", fake)

    real_read_candidates = tasks_mod._read_candidates
    spy = MagicMock(side_effect=real_read_candidates)
    monkeypatch.setattr(tasks_mod, "_read_candidates", spy)

    task_engine_tasks.propose_task_draft.apply(args=[USER_ID, job_id, "some goal"])

    spy.assert_called_once()
    assert spy.call_args.kwargs == {
        "user_id": USER_ID, "exclude": set(),
        "limit": task_engine_tasks.PROPOSE_READ_CANDIDATES_LIMIT,
    }

    row = _get_job(session_factory, job_id)
    # both seeded threads came back through the recency-pool fallback and scored
    assert len(row.payload["positives"]) == 2


# ---------------------------------------------------------------------------
# Top-level exception -> mark_failed + re-raise (Phase 4.5 Task 3, mirrors
# backfill_task's identical guard)
# ---------------------------------------------------------------------------


def test_propose_task_draft_exception_marks_job_failed_and_reraises(
    session_factory, monkeypatch,
):
    _seed_user(session_factory)
    job_id = _seed_job(session_factory, goal="help me plan a wedding")

    published: list[tuple] = []
    monkeypatch.setattr(
        tasks_mod, "_publish",
        lambda user_id, event, payload: published.append((user_id, event, payload)),
    )

    fake = _fake_call_messages([_proposal_json()])
    monkeypatch.setattr(llm_client, "call_messages", fake)

    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    # Force the final write step to blow up -- exercises the top-level
    # try/except regardless of which retry path the LLM mock took.
    monkeypatch.setattr(task_engine_tasks.jobs_repo, "set_payload", _boom)

    with pytest.raises(RuntimeError, match="disk on fire"):
        task_engine_tasks.propose_task_draft.apply(
            args=[USER_ID, job_id, "help me plan a wedding"],
        )

    row = _get_job(session_factory, job_id)
    assert row.stage == "failed"
    assert "disk on fire" in row.error
    assert row.needs_user is False

    assert published == [(USER_ID, "job_updated", {"job_id": job_id})]


def test_propose_task_draft_missing_job_row_while_recording_failure_is_swallowed(
    session_factory, monkeypatch,
):
    """If the job row vanishes between the main run's fetch and the failure
    write (e.g. a concurrent hard-delete), `_record_job_failure` must log and
    swallow rather than let a secondary exception mask the original one, and
    the ORIGINAL exception must still propagate."""
    _seed_user(session_factory)
    job_id = _seed_job(session_factory, goal="help me plan a wedding")

    fake = _fake_call_messages([_proposal_json()])
    monkeypatch.setattr(llm_client, "call_messages", fake)
    monkeypatch.setattr(tasks_mod, "_publish", lambda *a, **kw: None)

    def _boom_after_deleting_job(db, *, job, payload):
        # Delete the row for real, on a SEPARATE session/connection, right
        # before raising -- by the time _record_job_failure opens its own
        # fresh session, get_owned_job genuinely finds nothing.
        del_db = session_factory()
        row = del_db.get(Job, job.id)
        if row is not None:
            del_db.delete(row)
            del_db.commit()
        del_db.close()
        raise RuntimeError("original failure")

    monkeypatch.setattr(task_engine_tasks.jobs_repo, "set_payload", _boom_after_deleting_job)

    with pytest.raises(RuntimeError, match="original failure"):
        task_engine_tasks.propose_task_draft.apply(
            args=[USER_ID, job_id, "help me plan a wedding"],
        )
