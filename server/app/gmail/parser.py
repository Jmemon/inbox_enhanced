"""Gmail MessagePart decoder + thread assembler.

Lives in services because both workers (sync path) and any future api code
that wants to render a thread can use it. Stateless.

Real Gmail traffic is overwhelmingly multipart: a typical message has
mimeType=multipart/alternative at the top with text/plain and text/html
children, and the top-level body.data is empty. Messages with attachments
nest the alternative inside a multipart/mixed. So we walk payload.parts
recursively, preferring text/plain and falling back to text/html.

Attachments (parts with body.attachmentId instead of body.data) are skipped —
fetching them requires a separate users.messages.attachments.get call and the
homepage list view doesn't render them.

References:
 - https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages#Message
 - https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages#MessagePart
"""

import base64
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ParsedMessage:
    gmail_message_id: str
    gmail_thread_id: str
    gmail_internal_date: int
    gmail_history_id: str
    subject: str | None
    from_addr: str | None
    to_addr: str | None
    body_text: str    # full decoded body (used by classifier; also persisted as of Task 2)
    body_preview: str # first 150 chars (persisted; what UI renders)
    # Gmail labelIds snapshot (INBOX/UNREAD interpreted at persist time).
    # default_factory so existing ParsedMessage(...) constructions stay valid.
    label_ids: list[str] = field(default_factory=list)


@dataclass
class ParsedThread:
    gmail_thread_id: str
    subject: str | None
    recent_internal_date: int
    messages: list[ParsedMessage]


def _b64url_decode(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except Exception as exc:
        # binascii.Error means the gmail payload's base64 is corrupt; we want a
        # signal in worker logs so we can spot bad data, not silent ""s.
        log.warning("b64url_decode failed: %s", exc)
        return ""


def _header(payload: dict, name: str) -> str | None:
    target = name.lower()
    for h in payload.get("headers", []) or []:
        if h.get("name", "").lower() == target:
            return h.get("value")
    return None


def _find_body_by_mime(payload: dict, mime_type: str) -> str | None:
    """Walk the MessagePart tree depth-first looking for a part with the given
    mimeType that has inline body data. Skips attachments (body.attachmentId).
    Returns the decoded text or None if no matching part is found.
    """
    if payload.get("mimeType") == mime_type:
        body = payload.get("body") or {}
        data = body.get("data")
        if data:
            return _b64url_decode(data)
    for part in payload.get("parts", []) or []:
        found = _find_body_by_mime(part, mime_type)
        if found is not None:
            return found
    return None


def _extract_body_text(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html if no plain part exists.
    Returns "" when nothing usable is present."""
    plain = _find_body_by_mime(payload, "text/plain")
    if plain is not None:
        return plain
    html = _find_body_by_mime(payload, "text/html")
    if html is not None:
        return html
    return ""


def parse_message(raw: dict) -> ParsedMessage:
    payload = raw.get("payload", {}) or {}
    body_text = _extract_body_text(payload)
    return ParsedMessage(
        gmail_message_id=raw["id"],
        gmail_thread_id=raw["threadId"],
        gmail_internal_date=int(raw.get("internalDate", "0") or 0),
        gmail_history_id=str(raw.get("historyId", "") or ""),
        subject=_header(payload, "Subject"),
        from_addr=_header(payload, "From"),
        to_addr=_header(payload, "To"),
        body_text=body_text,
        body_preview=body_text[:150],
        label_ids=list(raw.get("labelIds", []) or []),
    )


def assemble_thread(*, thread_id: str, raw_messages: list[dict]) -> ParsedThread:
    parsed = [parse_message(m) for m in raw_messages]
    if not parsed:
        return ParsedThread(gmail_thread_id=thread_id, subject=None, recent_internal_date=0, messages=[])
    parsed.sort(key=lambda m: m.gmail_internal_date)
    most_recent = parsed[-1]
    return ParsedThread(
        gmail_thread_id=thread_id,
        subject=parsed[0].subject,
        recent_internal_date=most_recent.gmail_internal_date,
        messages=parsed,
    )


def thread_to_string(thread: ParsedThread) -> str:
    """Plain-text representation of a thread, used as input to the classifier.

    Uses the full body_text (not body_preview) — the classifier needs to see the
    whole message to make an accurate routing decision, even though the UI only
    persists/displays the 100-char preview.
    """
    lines: list[str] = []
    lines.append(f"Thread subject: {thread.subject or '(no subject)'}")
    for m in thread.messages:
        lines.append("---")
        lines.append(f"From: {m.from_addr or ''}")
        lines.append(f"To: {m.to_addr or ''}")
        lines.append(f"Subject: {m.subject or ''}")
        lines.append("")
        lines.append(m.body_text)
    return "\n".join(lines)
