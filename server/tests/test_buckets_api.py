from datetime import datetime, timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.db.models import Base, User, Task
from app.db.session import get_db
from app.auth import sessions


@pytest.fixture
def authed(tmp_path):
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path}/t.db", future=True)
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    def _get_db():
        s = TS()
        try: yield s
        finally: s.close()
    app.dependency_overrides[get_db] = _get_db

    db = TS()
    db.add(User(id="u1", email="a@b.com", created_at=datetime.now(timezone.utc)))
    db.add(User(id="u2", email="c@d.com", created_at=datetime.now(timezone.utc)))
    db.add(Task(id="def", user_id=None, kind="bucket", name="Important", goal="",
                criteria="x", state_schema=None, status="active", version=1,
                is_deleted=False, created_at=datetime.now(timezone.utc)))
    db.add(Task(id="other", user_id="u2", kind="bucket", name="theirs", goal="",
                criteria="x", state_schema=None, status="active", version=1,
                is_deleted=False, created_at=datetime.now(timezone.utc)))
    db.commit()
    sid = sessions.create_session(db, user_id="u1", ttl_seconds=600)
    c = TestClient(app); c.cookies.set("session", sid)
    # POST /api/buckets enqueues backfill_task (Phase 4 Task 2: buckets are
    # tasks(kind='bucket'), backfilled the same way a fresh tracker is);
    # without a real broker, apply_async would fail. The lifecycle test only
    # cares that the bucket was created — backfill correctness is exercised
    # separately (test_task_engine_tasks.py).
    with patch("app.api.buckets.task_engine_tasks.backfill_task.apply_async"):
        yield c
    app.dependency_overrides.clear(); eng.dispose()


def test_full_lifecycle(authed):
    # GET shows defaults, list shape matches the old bucket contract
    # byte-for-byte: {id, name, criteria, is_default}.
    r = authed.get("/api/buckets")
    assert r.status_code == 200
    default = next(b for b in r.json()["buckets"] if b["name"] == "Important")
    assert default["is_default"] is True
    assert set(default.keys()) == {"id", "name", "criteria", "is_default"}

    # POST creates
    r = authed.post("/api/buckets", json={
        "name": "Books", "description": "book club emails",
        "confirmed_positives": [{"sender": "club@b.com", "subject": "march pick",
                                 "snippet": "Beloved", "rationale": "club"}],
        "confirmed_negatives": [],
    })
    assert r.status_code == 201
    bid = r.json()["id"]
    assert r.json()["is_default"] is False
    assert "<positive>" in r.json()["criteria"]

    # PATCH renames
    r = authed.patch(f"/api/buckets/{bid}", json={"name": "Reading"})
    assert r.status_code == 200 and r.json()["name"] == "Reading"

    # DELETE soft-deletes; GET no longer shows it
    assert authed.delete(f"/api/buckets/{bid}").status_code == 204
    listed = authed.get("/api/buckets").json()["buckets"]
    assert all(b["id"] != bid for b in listed)


def test_delete_is_idempotent(authed):
    """Shim contract: a second DELETE on a bucket this user already deleted
    is a silent 204 no-op, not a 404 -- distinct from the never-existed/
    other-user cases, which stay 404/403."""
    c = authed
    r = c.post("/api/buckets", json={"name": "Once", "description": "d"})
    bid = r.json()["id"]
    assert c.delete(f"/api/buckets/{bid}").status_code == 204
    assert c.delete(f"/api/buckets/{bid}").status_code == 204


def test_draft_preview_routes_deleted(authed):
    """Phase 4 Task 3: the draft-preview machinery (POST/GET .../draft/
    preview*) and its preview_cache backing are gone entirely, superseded by
    the task engine's own goal->draft flow -- itself since retired in favor
    of the jobs surface (POST /api/jobs, Phase 4.5 Task 3). Neither
    path matches any surviving route in app/api/buckets.py -- the POST falls
    through to app.main's GET-only SPA catch-all, which matches the path but
    not the method (405, Starlette's standard "path exists, wrong verb"
    response); the polling GET falls through to that same catch-all's own
    explicit `api/`-prefix 404. NewBucketModal's existing `gone` polling
    handler already treats the GET path's 404 this way, so this is safe for
    the one-release shim window."""
    c = authed
    assert c.post("/api/buckets/draft/preview", json={
        "name": "X", "description": "d", "exclude_thread_ids": [],
    }).status_code == 405
    assert c.get("/api/buckets/draft/preview/some-draft-id").status_code == 404


def test_403_on_default_and_other_user(authed):
    assert authed.patch("/api/buckets/def", json={"name": "x"}).status_code == 403
    assert authed.delete("/api/buckets/def").status_code == 403
    assert authed.patch("/api/buckets/other", json={"name": "x"}).status_code == 403
    assert authed.delete("/api/buckets/other").status_code == 403


def test_unauth_returns_401():
    c = TestClient(app)
    assert c.get("/api/buckets").status_code == 401


def test_post_enqueues_backfill_task_not_reclassify(authed):
    """Phase 4 Task 2: POST /api/buckets must enqueue backfill_task for the
    new bucket's own task id with empty keyword_probes (a bucket has no
    LLM-proposed search terms the way a tracker wizard does) -- the old
    reclassify_user_inbox enqueue is gone entirely."""
    c = authed
    with patch("app.api.buckets.task_engine_tasks.backfill_task.apply_async") as mock_apply:
        r = c.post("/api/buckets", json={
            "name": "Receipts", "description": "purchase receipts",
        })
    assert r.status_code == 201
    bid = r.json()["id"]
    mock_apply.assert_called_once_with(args=["u1", bid, []], countdown=0)
