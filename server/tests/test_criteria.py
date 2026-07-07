"""task_engine.criteria tests: formulate_criteria's existing grammar (moved
here from test_bucket_repo.py's scope is NOT duplicated — see
test_task_repo.py::test_bucket_repo_formulate_criteria_is_the_relocated_function
for the shim assertion) plus Phase 2B Task 2's incremental learning-loop
additions: append_example / cap_examples (spec §4.6).
"""

import re

from app.task_engine.criteria import (
    EXAMPLE_CAP,
    append_example,
    cap_examples,
    formulate_criteria,
)


def _count_blocks(text: str) -> int:
    return len(re.findall(r"<(?:positive|nearmiss)>", text))


def _example(n: int, **overrides) -> dict:
    ex = {
        "sender": f"sender{n}@example.com",
        "subject": f"subject {n}",
        "snippet": f"snippet body {n}",
        "rationale": f"rationale {n}",
    }
    ex.update(overrides)
    return ex


# ---------------------------------------------------------------------------
# append_example: byte-compatible with formulate_criteria's own grammar
# ---------------------------------------------------------------------------


def test_append_example_block_is_byte_identical_to_formulate_criteria_inline():
    """A criteria string built by formulate_criteria with one positive example
    baked in must be BYTE IDENTICAL to: formulate_criteria with no examples,
    then append_example of that same example — proving append_example's block
    grammar matches formulate_criteria's inline rendering exactly, not just
    approximately."""
    ex = _example(1)

    built_inline = formulate_criteria(
        description="d", confirmed_positives=[ex], confirmed_negatives=[],
    )

    built_incrementally = formulate_criteria(
        description="d", confirmed_positives=[], confirmed_negatives=[],
    )
    built_incrementally = append_example(built_incrementally, example=ex, tag="positive")

    assert built_incrementally == built_inline


def test_append_example_nearmiss_block_is_byte_identical_too():
    ex = _example(1)
    built_inline = formulate_criteria(
        description="d", confirmed_positives=[], confirmed_negatives=[ex],
    )
    base = formulate_criteria(description="d", confirmed_positives=[], confirmed_negatives=[])
    built_incrementally = append_example(base, example=ex, tag="nearmiss")
    assert built_incrementally == built_inline


def test_append_example_round_trip_formulate_then_append_parses_with_n_plus_one_blocks():
    """Mandatory round-trip test: a criteria built by formulate_criteria (n
    example blocks) plus one append_example call must parse as n+1 blocks."""
    positives = [_example(i) for i in range(3)]
    negatives = [_example(i, sender=f"neg{i}@x.com") for i in range(2)]
    base = formulate_criteria(
        description="Track my job hunt.",
        confirmed_positives=positives,
        confirmed_negatives=negatives,
    )
    n = _count_blocks(base)
    assert n == 5

    appended = append_example(
        base, example=_example(99, subject="brand new subject"), tag="positive",
    )
    assert _count_blocks(appended) == n + 1
    # description + header preserved
    assert appended.startswith("Track my job hunt.")
    assert "Example cases:" in appended
    # every prior block still present, plus the new one
    for i in range(3):
        assert f"subject {i}" in appended
    assert "brand new subject" in appended


def test_append_example_adds_example_cases_section_when_absent():
    """Legacy/hand-written criteria with a description but no 'Example
    cases:' section yet must gain one."""
    legacy = "Some hand-written description with no examples section."
    out = append_example(legacy, example=_example(1), tag="positive")
    assert "Example cases:" in out
    assert legacy in out
    assert _count_blocks(out) == 1
    assert out.index(legacy) < out.index("Example cases:") < out.index("<positive>")


def test_append_example_on_empty_criteria_still_produces_valid_block():
    out = append_example("", example=_example(1), tag="positive")
    assert "Example cases:" in out
    assert _count_blocks(out) == 1


def test_append_example_without_rationale_omits_why_line():
    out = append_example("d\n\nExample cases:\n", example=_example(1, rationale=""), tag="positive")
    assert "Why:" not in out


# ---------------------------------------------------------------------------
# cap_examples: FIFO drop of oldest blocks, preserving survivor order
# ---------------------------------------------------------------------------


def test_cap_examples_is_noop_when_under_cap():
    base = formulate_criteria(
        description="d", confirmed_positives=[_example(1)], confirmed_negatives=[],
    )
    assert cap_examples(base, cap=EXAMPLE_CAP) == base


def test_cap_examples_drops_oldest_preserving_description_and_survivor_order():
    positives = [_example(i) for i in range(5)]
    base = formulate_criteria(description="Keep me.", confirmed_positives=positives, confirmed_negatives=[])

    capped = cap_examples(base, cap=3)

    assert _count_blocks(capped) == 3
    assert capped.startswith("Keep me.")
    assert "Example cases:" in capped
    # oldest two (subject 0, subject 1) dropped
    assert "subject 0" not in capped
    assert "subject 1" not in capped
    # newest three survive, in original relative order
    assert capped.index("subject 2") < capped.index("subject 3") < capped.index("subject 4")


def test_cap_examples_drops_oldest_across_mixed_tags_in_document_order():
    """Blocks appended over time interleave positive/nearmiss in whatever
    order they actually occurred — cap_examples must drop the oldest by
    DOCUMENT position, not by grouping tag first."""
    base = formulate_criteria(description="d", confirmed_positives=[], confirmed_negatives=[])
    text = base
    # Append in a specific chronological order: pos(A), nearmiss(B), pos(C)
    text = append_example(text, example=_example(1, subject="A"), tag="positive")
    text = append_example(text, example=_example(2, subject="B"), tag="nearmiss")
    text = append_example(text, example=_example(3, subject="C"), tag="positive")

    capped = cap_examples(text, cap=2)
    assert _count_blocks(capped) == 2
    assert "Subject: A" not in capped  # oldest, dropped
    assert "Subject: B" in capped and "Subject: C" in capped
    assert capped.index("Subject: B") < capped.index("Subject: C")


def test_cap_examples_default_cap_is_30():
    positives = [_example(i) for i in range(35)]
    base = formulate_criteria(description="d", confirmed_positives=positives, confirmed_negatives=[])
    capped = cap_examples(base)
    assert _count_blocks(capped) == 30
    # newest 30 survive (subjects 5..34)
    assert "subject 4" not in capped
    assert "subject 5" in capped
    assert "subject 34" in capped


def test_append_example_enforces_cap_on_the_31st_append():
    text = formulate_criteria(
        description="d",
        confirmed_positives=[_example(i) for i in range(EXAMPLE_CAP)],
        confirmed_negatives=[],
    )
    assert _count_blocks(text) == EXAMPLE_CAP

    text = append_example(text, example=_example(999, subject="the newest one"), tag="nearmiss")
    assert _count_blocks(text) == EXAMPLE_CAP  # still capped, not 31
    assert "subject 0" not in text  # oldest got dropped to make room
    assert "the newest one" in text
