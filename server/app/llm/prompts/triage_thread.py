import json
from app.db.models import Task
from app.llm.prompts import classify_thread


# Extends classify_thread's discipline: same bucket-pick task, PLUS a
# multi-label pass over the user's tracked tasks in the SAME call (D2 — no
# doubled LLM volume). One Haiku call per thread does both jobs.
SYSTEM_PROMPT = """You triage email threads for two purposes at once.

1. Bucket: classify the thread into one of the given buckets. Each bucket has
a name and criteria text containing a description plus tagged <positive>
example messages that fit and <nearmiss> messages that look similar but do
NOT belong. Pick the single bucket whose criteria + positives best match the
thread. If a thread is borderline and no bucket clearly fits, prefer "no fit".

2. Tasks: independently decide which of the given tracked tasks (if any) this
thread is relevant to. Each task has a name and criteria text describing what
it tracks. A thread may be relevant to zero, one, or several tasks — this is
multi-label, not a single pick. Score each relevant task with a confidence
0-100 reflecting how clearly the thread matches that task's criteria. Omit
tasks the thread has no bearing on.

Output exactly one line of JSON, with no other text or code fences:
  {"bucket_name": "<bucket name copied exactly>"|null,
   "relevant_tasks": [{"name": "<task name copied exactly>", "confidence": <0-100 integer>}, ...]}
"""


def build_user_message(
    *, thread_str: str, buckets: list[Task], trackers: list[Task],
    current_bucket_name: str | None,
) -> str:
    """Renders the bucket section identically to classify_thread's own
    build_user_message (delegated, not reimplemented, so the zero-tracker
    case is byte-for-byte the same prompt classify() sends), then appends
    <task name="...">{criteria}</task> blocks for each active tracker."""
    base = classify_thread.build_user_message(
        thread_str=thread_str, buckets=buckets, current_bucket_name=current_bucket_name,
    )
    if not trackers:
        return base
    task_blocks = "\n\n".join(
        f'<task name="{t.name}">\n{t.criteria}\n</task>'
        for t in trackers
    )
    return f"{base}\n\nTracked tasks:\n\n{task_blocks}"


def parse_response(
    text: str, buckets: list[Task], trackers: list[Task],
) -> tuple[str | None, list[tuple[str, int]]]:
    """Resolve the model's bucket pick + tracked-task relevance against the
    same buckets/trackers lists shown in the prompt.

    Carries over classify_thread.parse_response's exact discipline: malformed
    JSON (or non-object JSON) -> (None, []); unknown or ambiguous
    (duplicate-name) bucket/task names are dropped rather than guessed at.
    Task confidences are clamped to 0-100 ints; a relevant_tasks entry with a
    non-numeric/missing confidence is dropped (same "don't guess" discipline
    as an unresolvable name) rather than defaulted to some invented value.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            text = text[s : e + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(data, dict):
        return None, []

    name = data.get("bucket_name")
    bucket_id: str | None = None
    if isinstance(name, str):
        matches = [b.id for b in buckets if b.name == name]
        if len(matches) == 1:
            bucket_id = matches[0]

    tasks_out: list[tuple[str, int]] = []
    seen_task_ids: set[str] = set()
    raw_tasks = data.get("relevant_tasks")
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            tname = item.get("name")
            if not isinstance(tname, str):
                continue
            matches = [t.id for t in trackers if t.name == tname]
            if len(matches) != 1:
                continue  # unknown or ambiguous duplicate-name -> dropped
            task_id = matches[0]
            if task_id in seen_task_ids:
                continue  # a repeated entry for the same task doesn't stack

            conf = item.get("confidence")
            if isinstance(conf, bool) or not isinstance(conf, (int, float)):
                continue  # malformed confidence -> drop, don't guess
            conf_int = max(0, min(100, int(round(conf))))

            seen_task_ids.add(task_id)
            tasks_out.append((task_id, conf_int))

    return bucket_id, tasks_out
