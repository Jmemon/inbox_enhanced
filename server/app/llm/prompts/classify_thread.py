import json
from app.db.models import Task


SYSTEM_PROMPT = """You classify email threads into buckets defined by criteria.
Each bucket has a name and criteria text containing a description plus
tagged <positive> example messages that fit and <nearmiss> messages that look
similar but do NOT belong.

Pick the single bucket whose criteria + positives best match the thread.
If a thread is borderline and no bucket clearly fits, prefer "no fit".

Output exactly one line of JSON, with no other text or code fences:
  {"bucket_name": "<bucket name copied exactly>"}
or
  {"bucket_name": null}
"""


def build_user_message(*, thread_str: str, buckets: list[Task], current_bucket_name: str | None) -> str:
    blocks = "\n\n".join(
        f'<bucket name="{b.name}">\n{b.criteria}\n</bucket>'
        for b in buckets
    )
    stability = ""
    if current_bucket_name:
        stability = (
            f'\n\nThis thread is currently classified as bucket "{current_bucket_name}". '
            "Only change the bucket if a different one is clearly more appropriate."
        )
    return f"Available buckets:\n\n{blocks}\n\nThread to classify:\n\n{thread_str}{stability}"


def parse_response(text: str, buckets: list[Task]) -> str | None:
    """Resolve the model's chosen bucket name to a bucket id using the same
    buckets list shown in the prompt. Returns None for null, unknown, or
    ambiguous (duplicate-name) responses — caller treats None as no-fit."""
    text = (text or "").strip()
    if text.startswith("```"):
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s: text = text[s : e + 1]
    try:
        name = json.loads(text).get("bucket_name")
    except json.JSONDecodeError:
        return None
    if not isinstance(name, str):
        return None
    matches = [b.id for b in buckets if b.name == name]
    return matches[0] if len(matches) == 1 else None
