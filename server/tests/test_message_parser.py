import base64
from app.gmail import parser as message_parser


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _flat_message(*, mid="m1", tid="t1", internal_date="1700000000000", history_id="42",
                  subject="hello", from_="a@x.com", to_="b@x.com", body_text="hi there",
                  mime_type="text/plain"):
    """A simple non-multipart message: body lives at payload.body.data (rare in
    real gmail traffic, but valid per the API)."""
    return {
        "id": mid, "threadId": tid, "internalDate": internal_date, "historyId": history_id,
        "payload": {
            "mimeType": mime_type,
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_},
                {"name": "To", "value": to_},
            ],
            "body": {"data": _b64url(body_text)},
        },
    }


def _multipart_alternative_message(*, body_text="plain version", html="<p>html version</p>"):
    """The shape Gmail returns for almost every real email: multipart/alternative
    at the top, with text/plain and text/html children. Top-level body is empty."""
    return {
        "id": "m1", "threadId": "t1", "internalDate": "1700000000000", "historyId": "42",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "real email"},
                {"name": "From", "value": "alice@x.com"},
                {"name": "To", "value": "me@x.com"},
            ],
            "body": {"size": 0},
            "parts": [
                {"mimeType": "text/plain", "headers": [], "body": {"data": _b64url(body_text)}},
                {"mimeType": "text/html", "headers": [], "body": {"data": _b64url(html)}},
            ],
        },
    }


def _multipart_mixed_with_attachment_message(*, body_text="see attached"):
    """Email with an attachment: top is multipart/mixed, body is in a nested
    multipart/alternative, attachment part has attachmentId not data."""
    return {
        "id": "m1", "threadId": "t1", "internalDate": "1700000000000", "historyId": "42",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "with attachment"},
                {"name": "From", "value": "alice@x.com"},
                {"name": "To", "value": "me@x.com"},
            ],
            "body": {"size": 0},
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "headers": [], "body": {"size": 0},
                    "parts": [
                        {"mimeType": "text/plain", "headers": [], "body": {"data": _b64url(body_text)}},
                        {"mimeType": "text/html", "headers": [], "body": {"data": _b64url("<p>html</p>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf", "filename": "doc.pdf",
                    "headers": [], "body": {"attachmentId": "ATT_xyz", "size": 12345},
                },
            ],
        },
    }


def test_parse_flat_text_plain_message_pulls_headers_and_body():
    parsed = message_parser.parse_message(_flat_message(body_text="line one line two"))
    assert parsed.gmail_message_id == "m1"
    assert parsed.gmail_thread_id == "t1"
    assert parsed.gmail_internal_date == 1700000000000
    assert parsed.gmail_history_id == "42"
    assert parsed.subject == "hello"
    assert parsed.from_addr == "a@x.com"
    assert parsed.to_addr == "b@x.com"
    assert parsed.body_preview.startswith("line one")


def test_parse_multipart_alternative_prefers_text_plain_part():
    parsed = message_parser.parse_message(_multipart_alternative_message())
    assert parsed.subject == "real email"
    assert parsed.body_preview.startswith("plain version")


def test_parse_multipart_mixed_with_attachment_recurses_into_alternative():
    parsed = message_parser.parse_message(_multipart_mixed_with_attachment_message())
    assert parsed.body_preview.startswith("see attached")


def test_parse_falls_back_to_text_html_when_no_text_plain_present():
    msg = {
        "id": "m1", "threadId": "t1", "internalDate": "1", "historyId": "1",
        "payload": {
            "mimeType": "multipart/related",
            "headers": [{"name": "Subject", "value": "html only"}],
            "body": {"size": 0},
            "parts": [
                {"mimeType": "text/html", "headers": [], "body": {"data": _b64url("<p>only html</p>")}},
            ],
        },
    }
    parsed = message_parser.parse_message(msg)
    # html fallback is acceptable; for v1 we don't strip tags, just take what we can
    assert "html" in parsed.body_preview


def test_body_preview_truncates_to_150_chars():
    long_body = "x" * 500
    parsed = message_parser.parse_message(_flat_message(body_text=long_body))
    assert len(parsed.body_preview) == 150


def test_missing_body_yields_empty_preview():
    msg = _flat_message()
    msg["payload"]["body"] = {}
    parsed = message_parser.parse_message(msg)
    assert parsed.body_preview == ""


def test_assemble_thread_picks_subject_from_first_message_and_recent_internaldate():
    msgs = [
        _flat_message(mid="m1", tid="t1", internal_date="1000", subject="re: x"),
        _flat_message(mid="m2", tid="t1", internal_date="2000", subject="re: x"),
    ]
    thread = message_parser.assemble_thread(thread_id="t1", raw_messages=msgs)
    assert thread.gmail_thread_id == "t1"
    assert thread.subject == "re: x"
    assert len(thread.messages) == 2
    assert thread.recent_internal_date == 2000


def test_thread_string_representation_for_classifier_includes_headers_and_bodies():
    msgs = [_flat_message(subject="re: roadmap", body_text="ship friday")]
    thread = message_parser.assemble_thread(thread_id="t1", raw_messages=msgs)
    text = message_parser.thread_to_string(thread)
    assert "Subject: re: roadmap" in text
    assert "ship friday" in text


def test_body_text_is_full_decoded_body_not_truncated():
    long = "x" * 500
    parsed = message_parser.parse_message(_flat_message(body_text=long))
    assert len(parsed.body_text) == 500
    assert len(parsed.body_preview) == 150


def test_thread_to_string_includes_full_bodies_for_classifier():
    """thread_to_string is the classifier's input — it must contain the full
    bodies, not the 150-char ui previews."""
    long_body = "x" * 500 + " UNIQUE_TAIL"
    msgs = [_flat_message(subject="thread", body_text=long_body)]
    thread = message_parser.assemble_thread(thread_id="t1", raw_messages=msgs)
    text = message_parser.thread_to_string(thread)
    assert "UNIQUE_TAIL" in text


def test_parse_message_captures_label_ids():
    raw = {
        "id": "m1", "threadId": "t1", "internalDate": "1000", "historyId": "5",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": [], "mimeType": "text/plain",
                    "body": {"data": "aGVsbG8="}},  # "hello"
    }
    from app.gmail.parser import parse_message
    m = parse_message(raw)
    assert m.label_ids == ["INBOX", "UNREAD"]
    assert m.body_text == "hello"
