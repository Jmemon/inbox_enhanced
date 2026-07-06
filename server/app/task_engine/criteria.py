"""Criteria-text construction shared by buckets and tasks.

Relocated verbatim from app.inbox.bucket_repo (Phase 2A Task 3) because
Phase 4 tasks reuse the exact same criteria grammar for their classify path.
app.inbox.bucket_repo keeps a `from app.task_engine.criteria import
formulate_criteria` re-export so api/buckets.py and existing bucket tests
keep working untouched; Phase 4 removes that shim once bucket_repo callers
are migrated to call here directly.
"""


def formulate_criteria(
    *,
    description: str,
    confirmed_positives: list[dict],
    confirmed_negatives: list[dict],
) -> str:
    """Build the final criteria text mirroring default-bucket structure.

    Each example in confirmed_* is a dict with keys:
       sender    — e.g. "alice@example.com"
       subject   — string
       snippet   — verbatim quotation from the thread that the LLM surfaced
                   and the user agreed with
       rationale — one-line LLM rationale the user approved

    The output is a description paragraph + "Example cases:" + tagged
    <positive>/<nearmiss> blocks in the same shape as default-bucket criteria
    (see app/llm/default_criteria.py).
    """
    lines: list[str] = [description.strip(), "", "Example cases:"]
    for ex in confirmed_positives:
        lines.append("<positive>")
        lines.append(f"From: {ex.get('sender', '')}")
        lines.append(f"To: me")
        lines.append(f"Subject: {ex.get('subject', '')}")
        lines.append("")
        lines.append(ex.get("snippet", ""))
        rationale = ex.get("rationale", "")
        if rationale:
            lines.append("")
            lines.append(f"Why: {rationale}")
        lines.append("</positive>")
    for ex in confirmed_negatives:
        lines.append("<nearmiss>")
        lines.append(f"From: {ex.get('sender', '')}")
        lines.append(f"To: me")
        lines.append(f"Subject: {ex.get('subject', '')}")
        lines.append("")
        lines.append(ex.get("snippet", ""))
        rationale = ex.get("rationale", "")
        if rationale:
            lines.append("")
            lines.append(f"Why: {rationale}")
        lines.append("</nearmiss>")
    return "\n".join(lines) + "\n"
