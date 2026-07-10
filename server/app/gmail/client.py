import base64
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from app.config import get_settings
from app.db.models import User
from app.auth import crypto, google_oauth


# Phase 5 (actions, spec 006 §1, §3): the two write scopes SCOPES gained
# alongside gmail.readonly. Kept as named constants (rather than re-deriving
# from google_oauth.SCOPES) so callers preflighting a specific write can name
# exactly the scope that call needs. NO gmail.send constant exists here or
# anywhere in this module -- see test_no_send_scope_or_send_path_exists.
WRITE_SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
WRITE_SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"


class MissingScopesError(Exception):
    """Raised by a Gmail write call (archive_thread/label_thread/create_draft)
    when require_scopes() finds the user's stored gmail_granted_scopes lack a
    scope the call needs. Always raised BEFORE any network call -- a missing
    scope never reaches the Gmail API, never consumes a retry, and never
    triggers an implicit consent prompt. The execute_action engine (Phase 5
    Task 3) catches this and marks the action row `failed` with str(self) as
    the error ("needs permission: ...").
    """

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        message = f"needs permission: missing {', '.join(missing)}"
        super().__init__(message)


def ensure_fresh_access_token(db: Session, user: User) -> str:
    """Returns a usable access token, refreshing and persisting if needed."""
    if (
        user.gmail_access_token
        and user.gmail_access_token_expires_at
        and user.gmail_access_token_expires_at > datetime.now(timezone.utc) + timedelta(seconds=60)
    ):
        return crypto.decrypt(user.gmail_access_token)

    if not user.gmail_refresh_token:
        raise RuntimeError("user has no refresh token; must re-auth")

    refresh_plain = crypto.decrypt(user.gmail_refresh_token)
    refreshed = google_oauth.refresh_access_token(refresh_token=refresh_plain)
    user.gmail_access_token = crypto.encrypt(refreshed.access_token)
    user.gmail_access_token_expires_at = refreshed.expires_at
    db.commit()
    return refreshed.access_token


def _credentials(access_token: str, refresh_token: str | None) -> Credentials:
    s = get_settings()
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        scopes=google_oauth.SCOPES,
    )


def get_gmail_client(db: Session, user: User):
    """Build an authenticated googleapiclient gmail v1 resource for the user.

    Both api routes (/profile probe) and celery workers go through here so
    refresh-on-demand behaves identically in both call sites.
    """
    access_token = ensure_fresh_access_token(db, user)
    refresh_plain = crypto.decrypt(user.gmail_refresh_token) if user.gmail_refresh_token else None
    creds = _credentials(access_token, refresh_plain)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_profile_summary(db: Session, user: User) -> dict:
    """Returns Gmail profile + first three message subjects to prove read access works."""
    gmail = get_gmail_client(db, user)
    profile = gmail.users().getProfile(userId="me").execute()
    listing = gmail.users().messages().list(userId="me", maxResults=3).execute()
    subjects: list[str] = []
    for m in listing.get("messages", []):
        full = (
            gmail.users()
            .messages()
            .get(userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject"])
            .execute()
        )
        headers = full.get("payload", {}).get("headers", [])
        subj = next((h["value"] for h in headers if h["name"].lower() == "subject"), "(no subject)")
        subjects.append(subj)
    return {
        "email": profile.get("emailAddress"),
        "messages_total": profile.get("messagesTotal"),
        "threads_total": profile.get("threadsTotal"),
        "recent_subjects": subjects,
    }


def require_scopes(user: User, *needed: str) -> str | None:
    """Pure scope preflight -- no IO, no exception. Returns an error message
    if `user.gmail_granted_scopes` (NULL treated as no write scopes granted:
    pre-migration accounts, or accounts that haven't re-consented since
    gmail.modify/gmail.compose were added to SCOPES) is missing any of
    `needed`, else None.

    Every write function below calls this first and raises
    MissingScopesError before building a Gmail client or making any request.
    """
    granted = set(user.gmail_granted_scopes or [])
    missing = [scope for scope in needed if scope not in granted]
    if not missing:
        return None
    return f"needs permission: missing {', '.join(missing)}"


def _require_scopes_missing(user: User, *needed: str) -> list[str] | None:
    """Internal helper: returns the structured list of missing scopes for
    MissingScopesError construction, or None if all scopes are granted.
    """
    granted = set(user.gmail_granted_scopes or [])
    missing = [scope for scope in needed if scope not in granted]
    return missing if missing else None


def _header(message: dict, name: str) -> str | None:
    headers = message.get("payload", {}).get("headers", [])
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), None)


def archive_thread(db: Session, user: User, gmail_thread_id: str) -> dict:
    """Removes the INBOX label from a thread. Reversible (undo re-adds it).

    Preflights WRITE_SCOPE_MODIFY; raises MissingScopesError if absent.
    Returns {"removed_label_ids": [...]} -- exactly what task_actions.result
    needs to construct the inverse for undo.
    """
    missing = _require_scopes_missing(user, WRITE_SCOPE_MODIFY)
    if missing:
        raise MissingScopesError(missing)

    gmail = get_gmail_client(db, user)
    gmail.users().threads().modify(
        userId="me", id=gmail_thread_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()
    return {"removed_label_ids": ["INBOX"]}


def label_thread(db: Session, user: User, gmail_thread_id: str, label_name: str) -> dict:
    """Applies a label to a thread, creating it if it doesn't already exist
    (case-insensitive name match against the user's existing labels).

    Preflights WRITE_SCOPE_MODIFY; raises MissingScopesError if absent.
    Returns {"added_label_ids": [...], "label_id": ..., "label_name": ...} --
    enough for task_actions.result to drive undo (remove the same id) without
    a second name lookup.
    """
    missing = _require_scopes_missing(user, WRITE_SCOPE_MODIFY)
    if missing:
        raise MissingScopesError(missing)

    gmail = get_gmail_client(db, user)
    existing = gmail.users().labels().list(userId="me").execute().get("labels", [])
    # Only match against user labels, never system labels (SPAM, TRASH, UNREAD,
    # STARRED, IMPORTANT, etc.). A rule param must never be able to silently
    # alias SPAM or move threads to trash.
    match = next(
        (lbl for lbl in existing if lbl["type"] == "user" and lbl["name"].lower() == label_name.lower()),
        None,
    )
    if match is not None:
        label_id = match["id"]
    else:
        # No matching user label; create a new one. Gmail forbids creating
        # names colliding with system display names; if the create call fails,
        # let the error propagate to the caller's failure handling.
        created = gmail.users().labels().create(userId="me", body={"name": label_name}).execute()
        label_id = created["id"]

    gmail.users().threads().modify(
        userId="me", id=gmail_thread_id, body={"addLabelIds": [label_id]}
    ).execute()
    return {"added_label_ids": [label_id], "label_id": label_id, "label_name": label_name}


def create_draft(db: Session, user: User, gmail_thread_id: str, body_text: str) -> dict:
    """Creates a Gmail **draft** reply on a thread -- never sends anything
    (no gmail.send scope or code path exists anywhere in this app).

    Replies to the thread's latest message NOT from the user (falls back to
    the latest message overall if every message is the user's own), threading
    In-Reply-To/References off that message's Message-ID and prefixing
    "Re: " onto its Subject unless already present.

    Preflights WRITE_SCOPE_COMPOSE; raises MissingScopesError if absent.
    Returns {"draft_id": ...}.
    """
    missing = _require_scopes_missing(user, WRITE_SCOPE_COMPOSE)
    if missing:
        raise MissingScopesError(missing)

    gmail = get_gmail_client(db, user)
    thread = gmail.users().threads().get(
        userId="me",
        id=gmail_thread_id,
        format="metadata",
        metadataHeaders=["From", "To", "Subject", "Message-ID"],
    ).execute()
    messages = thread.get("messages", [])
    if not messages:
        raise RuntimeError(f"gmail thread {gmail_thread_id} has no messages to reply to")

    reply_to = next(
        (m for m in reversed(messages) if user.email.lower() not in (_header(m, "From") or "").lower()),
        messages[-1],
    )

    from_addr = _header(reply_to, "From") or ""
    subject = _header(reply_to, "Subject") or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id = _header(reply_to, "Message-ID")

    reply = MIMEText(body_text)
    reply["To"] = from_addr
    reply["Subject"] = subject
    if message_id:
        reply["In-Reply-To"] = message_id
        reply["References"] = message_id

    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    draft = gmail.users().drafts().create(
        userId="me", body={"message": {"threadId": gmail_thread_id, "raw": raw}}
    ).execute()
    return {"draft_id": draft["id"]}
