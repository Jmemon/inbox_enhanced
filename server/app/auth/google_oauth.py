from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from app.config import get_settings


# Phase 5 (actions, spec 006 §1): full write scopes at signup — the GCP
# consent screen already lists gmail.modify + gmail.compose. NO gmail.send:
# its absence here is a tested invariant (test_gmail_writes.py), not an
# oversight — the app never sends mail on the user's behalf, only drafts.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]


@dataclass
class ExchangedTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    email: str
    name: str | None
    # The token response's actual granted scopes (never the requested SCOPES
    # list) -- see exchange_code(). Defaults to [] so call sites/tests that
    # don't care about scopes (login/profile flows) need no changes.
    granted_scopes: list[str] = field(default_factory=list)


@dataclass
class RefreshedTokens:
    access_token: str
    expires_at: datetime


def _flow() -> Flow:
    s = get_settings()
    return Flow.from_client_config(
        client_config={
            "web": {
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [s.google_redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=s.google_redirect_uri,
    )


def build_authorize_url(*, state: str) -> str:
    s = get_settings()
    # Build the URL by hand so this function stays pure (no network) and unit-testable.
    params = {
        "response_type": "code",
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urlencode(params)


def _exchange(code: str) -> Credentials:
    flow = _flow()
    flow.fetch_token(code=code)
    return flow.credentials


def _fetch_userinfo(creds: Credentials) -> dict:
    service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    return service.userinfo().get().execute()


def exchange_code(*, code: str) -> ExchangedTokens:
    creds = _exchange(code)
    userinfo = _fetch_userinfo(creds)
    expiry = creds.expiry  # naive UTC per google library
    expires_at = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry
    # google-auth populates Credentials.granted_scopes from the token response's
    # own `scope` field (google_auth_oauthlib.helpers.credentials_from_session),
    # which can differ from what we requested (partial consent) or be omitted
    # entirely by the token endpoint when it matches the request exactly. Either
    # way we store only what came back, never SCOPES itself.
    granted_scopes = list(creds.granted_scopes) if creds.granted_scopes else []
    return ExchangedTokens(
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expires_at=expires_at,
        email=userinfo["email"],
        name=userinfo.get("name"),
        granted_scopes=granted_scopes,
    )


def _refresh(refresh_token: str) -> Credentials:
    s = get_settings()
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return creds


def refresh_access_token(*, refresh_token: str) -> RefreshedTokens:
    creds = _refresh(refresh_token)
    expiry = creds.expiry
    expires_at = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry
    return RefreshedTokens(access_token=creds.token, expires_at=expires_at)
