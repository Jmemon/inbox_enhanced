"""Criteria-text construction shared by buckets and tasks.

Relocated verbatim from app.inbox.bucket_repo (Phase 2A Task 3) because
Phase 4 tasks reuse the exact same criteria grammar for their classify path.
app.inbox.bucket_repo keeps a `from app.task_engine.criteria import
formulate_criteria` re-export so api/buckets.py and existing bucket tests
keep working untouched; Phase 4 removes that shim once bucket_repo callers
are migrated to call here directly.

Phase 2B Task 2 (spec §4.6, learning loop) adds the incremental side of this
grammar: `append_example` renders one new tagged block on top of an existing
criteria string (a user's attach/detach correction, fed back into the task's
own classify criteria) and `cap_examples` bounds the resulting example count
so criteria text can't grow unboundedly over a task's lifetime. Both are
built on `_render_block`, the exact per-example renderer `formulate_criteria`
itself uses — sharing that helper is what keeps the two paths byte-compatible
by construction rather than by convention.
"""

import re

# FIFO cap across positives+nearmisses combined (Task 2 default). Applied by
# append_example after every incremental append; also exposed standalone for
# tests / any future bulk-cap caller.
EXAMPLE_CAP = 30

# Matches one <positive>...</positive> or <nearmiss>...</nearmiss> block, in
# document order. Non-greedy + DOTALL + backreference so it can't bridge two
# separate blocks of different tags, and it doesn't matter how many/what
# lines live inside — this only ever needs to find the outer tag pair.
_BLOCK_RE = re.compile(r"<(positive|nearmiss)>.*?</\1>", re.DOTALL)


def _render_block(example: dict, tag: str) -> str:
    """Render ONE <tag>...</tag> example block, byte-identical to the shape
    `formulate_criteria` builds inline for each of its confirmed_* entries
    (From/To: me/Subject/blank/snippet/optional blank+Why/close). Shared by
    both `formulate_criteria` (batch construction) and `append_example`
    (single-example incremental append) so neither can drift out of sync with
    the other's grammar.
    """
    lines = [
        f"<{tag}>",
        f"From: {example.get('sender', '')}",
        "To: me",
        f"Subject: {example.get('subject', '')}",
        "",
        example.get("snippet", ""),
    ]
    rationale = example.get("rationale", "")
    if rationale:
        lines.append("")
        lines.append(f"Why: {rationale}")
    lines.append(f"</{tag}>")
    return "\n".join(lines)


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
        lines.append(_render_block(ex, "positive"))
    for ex in confirmed_negatives:
        lines.append(_render_block(ex, "nearmiss"))
    return "\n".join(lines) + "\n"


def cap_examples(criteria: str, *, cap: int = EXAMPLE_CAP) -> str:
    """Bound the number of <positive>/<nearmiss> blocks in `criteria` to
    `cap`, dropping the OLDEST blocks (by document order — blocks are only
    ever appended at the end, so document order is chronological order)
    while preserving the relative order of survivors. The description and
    the "Example cases:" header (everything before the first block) is left
    untouched, as is any trailing text after the last block.

    A no-op (returns `criteria` unchanged) when there are `cap` or fewer
    blocks already — including the common case of zero blocks (legacy
    criteria with no examples at all).
    """
    matches = list(_BLOCK_RE.finditer(criteria))
    if len(matches) <= cap:
        return criteria

    survivors = matches[max(0, len(matches) - cap):]
    header = criteria[: matches[0].start()]
    tail = criteria[matches[-1].end():]
    body = "\n".join(m.group(0) for m in survivors)
    return header + body + tail


def append_example(criteria: str, *, example: dict, tag: str) -> str:
    """Append one tagged example block (tag ∈ {"positive", "nearmiss"}) to an
    existing task's criteria text, then re-cap to EXAMPLE_CAP.

    This is the write side of spec §4.6's learning loop: a user's attach
    (positive) or detach (nearmiss) correction becomes one more tagged
    example the task's own classify/relevance pass sees on the next run,
    exactly the way `formulate_criteria`'s original confirmed_positives/
    confirmed_negatives examples do.

    If `criteria` has no "Example cases:" section yet (legacy criteria
    created before this section existed, or an empty string), one is added
    first — after a blank line if there's a non-empty description to
    preserve, with none otherwise.
    """
    block = _render_block(example, tag)
    base = criteria.rstrip("\n")
    if "Example cases:" not in criteria:
        prefix = f"{base}\n\n" if base else ""
        new_criteria = f"{prefix}Example cases:\n{block}\n"
    else:
        new_criteria = f"{base}\n{block}\n"
    return cap_examples(new_criteria)
