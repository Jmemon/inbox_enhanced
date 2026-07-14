from datetime import datetime, timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.db.models import Base, User
from app.db.session import get_db
from app.auth import google_oauth


@pytest.fixture
def client(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/test.db", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _get_db_override():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db_override
    with TestClient(app) as c:
        yield c, TestSession
    app.dependency_overrides.clear()
    engine.dispose()


def test_login_redirects_to_google_with_state_cookie(client):
    c, _ = client
    r = c.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "state=" in loc and "access_type=offline" in loc and "prompt=consent" in loc
    assert "oauth_state" in r.cookies


def test_callback_happy_path_creates_user_and_session_then_me_returns_user(client):
    c, TestSession = client

    # Kick off /auth/login to get a real state
    login = c.get("/auth/login", follow_redirects=False)
    state_in_url = _extract_state(login.headers["location"])

    fake_exchange = google_oauth.ExchangedTokens(
        access_token="ya29.fake",
        refresh_token="1//fake-refresh",
        expires_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        email="alice@example.com",
        name="Alice",
    )

    with patch("app.api.auth.google_oauth.exchange_code", return_value=fake_exchange):
        r = c.get(
            f"/auth/callback?code=auth-code-xyz&state={state_in_url}",
            follow_redirects=False,
        )

    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert "session" in r.cookies

    # User + session persisted, refresh token stored encrypted (not raw)
    with TestSession() as db:
        u = db.query(User).filter_by(email="alice@example.com").one()
        assert u.gmail_refresh_token and u.gmail_refresh_token != "1//fake-refresh"

    # /auth/me reflects the new session
    me = c.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "alice@example.com" and body["name"] == "Alice"


def test_callback_stores_exactly_googles_returned_scopes(client):
    c, TestSession = client
    login = c.get("/auth/login", follow_redirects=False)
    state_in_url = _extract_state(login.headers["location"])

    granted = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    fake_exchange = google_oauth.ExchangedTokens(
        access_token="ya29.fake",
        refresh_token="1//fake-refresh",
        expires_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        email="bob@example.com",
        name="Bob",
        granted_scopes=granted,
    )
    with patch("app.api.auth.google_oauth.exchange_code", return_value=fake_exchange):
        c.get(f"/auth/callback?code=x&state={state_in_url}", follow_redirects=False)

    with TestSession() as db:
        u = db.query(User).filter_by(email="bob@example.com").one()
        # Stores exactly what Google returned -- never the requested SCOPES list.
        assert u.gmail_granted_scopes == granted

    me = c.get("/auth/me")
    assert me.json()["has_write_scopes"] is True


def test_callback_overwrites_scopes_on_recall(client):
    """Re-consent (or a scope downgrade) must overwrite, not merge with, the
    previously-stored list -- every callback writes exactly what Google just
    returned."""
    c, TestSession = client
    login = c.get("/auth/login", follow_redirects=False)
    state_in_url = _extract_state(login.headers["location"])
    first = google_oauth.ExchangedTokens(
        access_token="ya29.fake",
        refresh_token="1//fake-refresh",
        expires_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        email="carol@example.com",
        name="Carol",
        granted_scopes=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
    )
    with patch("app.api.auth.google_oauth.exchange_code", return_value=first):
        c.get(f"/auth/callback?code=x&state={state_in_url}", follow_redirects=False)

    login2 = c.get("/auth/login", follow_redirects=False)
    state2 = _extract_state(login2.headers["location"])
    second = google_oauth.ExchangedTokens(
        access_token="ya29.fake2",
        refresh_token="1//fake-refresh",
        expires_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        email="carol@example.com",
        name="Carol",
        granted_scopes=["https://www.googleapis.com/auth/gmail.modify"],  # compose revoked
    )
    with patch("app.api.auth.google_oauth.exchange_code", return_value=second):
        c.get(f"/auth/callback?code=y&state={state2}", follow_redirects=False)

    with TestSession() as db:
        u = db.query(User).filter_by(email="carol@example.com").one()
        assert u.gmail_granted_scopes == ["https://www.googleapis.com/auth/gmail.modify"]

    me = c.get("/auth/me")
    assert me.json()["has_write_scopes"] is False


def test_me_has_write_scopes_false_when_scopes_null(client):
    """NULL gmail_granted_scopes (pre-migration accounts, or never re-consented)
    reads as no write scopes granted -- never a crash, never assumed-true."""
    c, TestSession = client
    login = c.get("/auth/login", follow_redirects=False)
    state_in_url = _extract_state(login.headers["location"])
    fake_exchange = google_oauth.ExchangedTokens(
        access_token="ya29.fake",
        refresh_token="1//fake-refresh",
        expires_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        email="dave@example.com",
        name="Dave",
    )
    with patch("app.api.auth.google_oauth.exchange_code", return_value=fake_exchange):
        c.get(f"/auth/callback?code=x&state={state_in_url}", follow_redirects=False)

    # Force the column to NULL directly -- distinct from "[]" (both read as
    # false, but NULL is the actual pre-migration/no-callback-yet state).
    with TestSession() as db:
        u = db.query(User).filter_by(email="dave@example.com").one()
        u.gmail_granted_scopes = None
        db.commit()

    me = c.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["has_write_scopes"] is False


def test_callback_with_google_error_redirects_with_authError(client):
    c, _ = client
    r = c.get("/auth/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/?authError=denied"


def test_me_returns_401_without_session(client):
    c, _ = client
    r = c.get("/auth/me")
    assert r.status_code == 401


def _extract_state(location: str) -> str:
    from urllib.parse import urlparse, parse_qs
    return parse_qs(urlparse(location).query)["state"][0]


def test_refresh_never_requests_scopes(monkeypatch):
    """Regression (Phase 5 gate): _refresh must construct Credentials with
    scopes=None. google-auth sends a `scope` param on the refresh grant when
    scopes is set, and Google rejects any refresh requesting scopes beyond
    the token's original grant with invalid_scope — which bricked every
    Gmail read for pre-Phase-5 accounts the moment SCOPES widened. Refresh
    must always run under the token's original grant."""
    from datetime import datetime, timezone

    from app.auth import google_oauth

    captured = {}

    class _FakeCreds:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.token = "fresh-access"
            self.expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)

        def refresh(self, request):  # noqa: ARG002 - signature parity
            pass

    monkeypatch.setattr(google_oauth, "Credentials", _FakeCreds)
    out = google_oauth.refresh_access_token(refresh_token="1//old-grant")
    assert "scopes" in captured and captured["scopes"] is None
    assert out.access_token == "fresh-access"
