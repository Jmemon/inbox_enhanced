import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.deps import get_current_user
from app.auth import crypto, google_oauth, sessions, state_cookie
from app.gmail.client import WRITE_SCOPE_COMPOSE, WRITE_SCOPE_MODIFY


router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"


def _cookie_kwargs(*, max_age: int | None = None) -> dict:
    s = get_settings()
    kw: dict = {
        "httponly": True,
        "secure": s.cookie_secure,
        "samesite": "lax",
        "path": "/",
    }
    if s.cookie_domain:
        kw["domain"] = s.cookie_domain
    if max_age is not None:
        kw["max_age"] = max_age
    return kw


# Mirror the secure/samesite/domain attributes from the original Set-Cookie when
# deleting. Strict browsers (Chrome) refuse to clear a cookie when the deletion
# header's attributes don't match, which would otherwise leave session cookies
# stuck after logout in production (where cookie_secure=True or COOKIE_DOMAIN is set).
def _delete_cookie(resp: Response, name: str) -> None:
    s = get_settings()
    resp.delete_cookie(
        name,
        path="/",
        domain=s.cookie_domain,
        secure=s.cookie_secure,
        samesite="lax",
    )


@router.get("/login")
def login() -> Response:
    raw, signed = state_cookie.make_state()
    url = google_oauth.build_authorize_url(state=signed)
    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie(STATE_COOKIE, raw, max_age=600, **_cookie_kwargs())
    return resp


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    oauth_state: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Response:
    if error:
        reason = "denied" if error == "access_denied" else error
        resp = RedirectResponse(url=f"/?authError={reason}", status_code=302)
        _delete_cookie(resp, STATE_COOKIE)
        return resp

    if not state_cookie.verify_state(cookie_value=oauth_state, url_value=state):
        raise HTTPException(status_code=400, detail="invalid state")

    if not code:
        raise HTTPException(status_code=400, detail="missing code")

    tokens = google_oauth.exchange_code(code=code)

    # Upsert user
    user = db.query(User).filter_by(email=tokens.email).one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4().hex,
            email=tokens.email,
            name=tokens.name,
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)

    # Encrypt and persist tokens
    if tokens.refresh_token:
        user.gmail_refresh_token = crypto.encrypt(tokens.refresh_token)
    user.gmail_access_token = crypto.encrypt(tokens.access_token)
    user.gmail_access_token_expires_at = tokens.expires_at
    # Phase 5 (actions, spec 006 §1): record exactly what Google granted on
    # EVERY callback (login and re-consent alike) -- never the requested
    # SCOPES list. Drives has_write_scopes below and every Gmail-write
    # preflight (app.gmail.client.require_scopes).
    user.gmail_granted_scopes = tokens.granted_scopes
    if tokens.name and not user.name:
        user.name = tokens.name
    db.commit()

    # Create session
    s = get_settings()
    sid = sessions.create_session(db, user_id=user.id, ttl_seconds=s.session_ttl_seconds)

    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, sid, max_age=s.session_ttl_seconds, **_cookie_kwargs())
    _delete_cookie(resp, STATE_COOKIE)
    return resp


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    # One flag, not two: the migration banner story is all-or-nothing since
    # new signups grant both write scopes together (spec 006 §1). NULL
    # granted-scopes (pre-migration, or never re-consented) reads as false.
    granted = set(user.gmail_granted_scopes or [])
    has_write_scopes = WRITE_SCOPE_MODIFY in granted and WRITE_SCOPE_COMPOSE in granted
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "has_write_scopes": has_write_scopes,
    }


@router.post("/logout", status_code=204)
def logout(
    session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Response:
    if session:
        sessions.revoke_session(db, session_id=session)
    resp = Response(status_code=204)
    _delete_cookie(resp, SESSION_COOKIE)
    return resp
