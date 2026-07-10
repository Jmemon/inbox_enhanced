"""Gmail write client (Phase 5 actions, spec 006 §3): archive_thread,
label_thread, create_draft, and the scope-preflight helper they all share.

Mocking style mirrors test_partial_sync.py: a MagicMock stands in for the
googleapiclient `gmail` resource, patched in via `get_gmail_client` (never a
real network call). Preflight-missing tests assert the mock is never touched
at all — a missing scope must short-circuit before any client is built.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import base64
import pytest

from app.db.models import Base, User
from app.gmail import client as gmail_client
from app.gmail.client import (
    WRITE_SCOPE_COMPOSE,
    WRITE_SCOPE_MODIFY,
    MissingScopesError,
    archive_thread,
    create_draft,
    label_thread,
    require_scopes,
)
from app.auth import google_oauth


def _user(db, *, email="me@example.com", granted_scopes=None) -> User:
    Base.metadata.create_all(db.get_bind())
    u = User(
        id="u1",
        email=email,
        created_at=datetime.now(timezone.utc),
        gmail_granted_scopes=granted_scopes,
    )
    db.add(u)
    db.commit()
    return u


def _both_scopes():
    return [WRITE_SCOPE_MODIFY, WRITE_SCOPE_COMPOSE]


# ---------------------------------------------------------------------------
# require_scopes — pure, no IO
# ---------------------------------------------------------------------------

def test_require_scopes_none_when_all_present(db):
    user = _user(db, granted_scopes=_both_scopes())
    assert require_scopes(user, WRITE_SCOPE_MODIFY) is None
    assert require_scopes(user, WRITE_SCOPE_MODIFY, WRITE_SCOPE_COMPOSE) is None


def test_require_scopes_message_when_missing(db):
    user = _user(db, granted_scopes=[WRITE_SCOPE_MODIFY])
    error = require_scopes(user, WRITE_SCOPE_COMPOSE)
    assert error is not None
    assert "compose" in error.lower() or WRITE_SCOPE_COMPOSE in error


def test_require_scopes_null_column_treated_as_no_scopes(db):
    user = _user(db, granted_scopes=None)
    assert require_scopes(user, WRITE_SCOPE_MODIFY) is not None
    assert require_scopes(user, WRITE_SCOPE_COMPOSE) is not None


# ---------------------------------------------------------------------------
# archive_thread
# ---------------------------------------------------------------------------

def test_archive_thread_happy_path(db):
    user = _user(db, granted_scopes=_both_scopes())
    gmail = MagicMock()
    gmail.users().threads().modify().execute.return_value = {}

    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        result = archive_thread(db, user, "gmail-thread-1")

    assert result == {"removed_label_ids": ["INBOX"]}
    gmail.users().threads().modify.assert_called_with(
        userId="me", id="gmail-thread-1", body={"removeLabelIds": ["INBOX"]}
    )


def test_archive_thread_missing_scope_raises_without_touching_api(db):
    user = _user(db, granted_scopes=None)
    gmail = MagicMock()

    with patch("app.gmail.client.get_gmail_client", return_value=gmail) as get_client:
        with pytest.raises(MissingScopesError):
            archive_thread(db, user, "gmail-thread-1")

    get_client.assert_not_called()
    gmail.users().threads().modify.assert_not_called()


# ---------------------------------------------------------------------------
# label_thread
# ---------------------------------------------------------------------------

def test_label_thread_creates_new_label_when_not_found(db):
    user = _user(db, granted_scopes=_both_scopes())
    gmail = MagicMock()
    gmail.users().labels().list().execute.return_value = {"labels": [{"id": "Label_1", "name": "Other"}]}
    gmail.users().labels().create().execute.return_value = {"id": "Label_new"}
    gmail.users().threads().modify().execute.return_value = {}

    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        result = label_thread(db, user, "gmail-thread-1", "Important")

    assert result == {"added_label_ids": ["Label_new"], "label_id": "Label_new", "label_name": "Important"}
    gmail.users().labels().create.assert_called_with(userId="me", body={"name": "Important"})
    gmail.users().threads().modify.assert_called_with(
        userId="me", id="gmail-thread-1", body={"addLabelIds": ["Label_new"]}
    )


def test_label_thread_reuses_existing_label_case_insensitive(db):
    user = _user(db, granted_scopes=_both_scopes())
    gmail = MagicMock()
    gmail.users().labels().list().execute.return_value = {
        "labels": [{"id": "Label_1", "name": "important"}]
    }
    gmail.users().threads().modify().execute.return_value = {}

    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        result = label_thread(db, user, "gmail-thread-1", "Important")

    assert result == {"added_label_ids": ["Label_1"], "label_id": "Label_1", "label_name": "Important"}
    gmail.users().labels().create.assert_not_called()
    gmail.users().threads().modify.assert_called_with(
        userId="me", id="gmail-thread-1", body={"addLabelIds": ["Label_1"]}
    )


def test_label_thread_missing_scope_raises_without_touching_api(db):
    user = _user(db, granted_scopes=[WRITE_SCOPE_COMPOSE])  # modify missing
    gmail = MagicMock()

    with patch("app.gmail.client.get_gmail_client", return_value=gmail) as get_client:
        with pytest.raises(MissingScopesError):
            label_thread(db, user, "gmail-thread-1", "Important")

    get_client.assert_not_called()
    gmail.users().labels().list.assert_not_called()


# ---------------------------------------------------------------------------
# create_draft
# ---------------------------------------------------------------------------

def _header_msg(msg_id: str, headers: dict) -> dict:
    return {
        "id": msg_id,
        "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]},
    }


def test_create_draft_replies_to_latest_non_self_message(db):
    user = _user(db, email="me@example.com", granted_scopes=_both_scopes())
    gmail = MagicMock()
    thread_payload = {
        "id": "gmail-thread-1",
        "messages": [
            _header_msg("m1", {"From": "them@example.com", "Subject": "Hello", "Message-ID": "<m1@mail>"}),
            _header_msg("m2", {"From": "me@example.com", "Subject": "Re: Hello", "Message-ID": "<m2@mail>"}),
        ],
    }
    gmail.users().threads().get().execute.return_value = thread_payload
    gmail.users().drafts().create().execute.return_value = {"id": "draft-1"}

    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        result = create_draft(db, user, "gmail-thread-1", "Sounds good, thanks!")

    assert result == {"draft_id": "draft-1"}

    create_call = gmail.users().drafts().create.call_args
    assert create_call.kwargs["userId"] == "me"
    body = create_call.kwargs["body"]
    assert body["message"]["threadId"] == "gmail-thread-1"
    raw_bytes = base64.urlsafe_b64decode(body["message"]["raw"])
    raw_text = raw_bytes.decode()
    assert "them@example.com" in raw_text  # replies to the non-self sender
    assert "<m1@mail>" in raw_text  # In-Reply-To / References tie to that message
    assert "Sounds good, thanks!" in raw_text
    assert "Re: Hello" in raw_text
    assert raw_text.count("Re: Re:") == 0  # no double Re: prefix


def test_create_draft_falls_back_to_latest_message_when_all_from_self(db):
    user = _user(db, email="me@example.com", granted_scopes=_both_scopes())
    gmail = MagicMock()
    thread_payload = {
        "id": "gmail-thread-1",
        "messages": [
            _header_msg("m1", {"From": "me@example.com", "Subject": "Note", "Message-ID": "<m1@mail>"}),
        ],
    }
    gmail.users().threads().get().execute.return_value = thread_payload
    gmail.users().drafts().create().execute.return_value = {"id": "draft-2"}

    with patch("app.gmail.client.get_gmail_client", return_value=gmail):
        result = create_draft(db, user, "gmail-thread-1", "Follow-up")

    assert result == {"draft_id": "draft-2"}
    body = gmail.users().drafts().create.call_args.kwargs["body"]
    raw_text = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
    assert "me@example.com" in raw_text


def test_create_draft_missing_scope_raises_without_touching_api(db):
    user = _user(db, granted_scopes=[WRITE_SCOPE_MODIFY])  # compose missing
    gmail = MagicMock()

    with patch("app.gmail.client.get_gmail_client", return_value=gmail) as get_client:
        with pytest.raises(MissingScopesError):
            create_draft(db, user, "gmail-thread-1", "body")

    get_client.assert_not_called()
    gmail.users().threads().get.assert_not_called()


# ---------------------------------------------------------------------------
# Safety invariant (spec 006 §6.3): no send capability anywhere.
# ---------------------------------------------------------------------------

def test_no_send_scope_or_send_path_exists():
    assert not any("send" in scope for scope in google_oauth.SCOPES)
    module_names = " ".join(dir(gmail_client)).lower()
    assert "send" not in module_names
