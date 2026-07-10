"""Unit tests for the pure Phase 5 action-rule evaluator (app.actions.rules).

Exhaustive per the task brief: each trigger x matching/non-matching params x
wrong-field events x deleted rules. Fakes are plain dataclasses, not the real
ORM models — app.actions.rules is pure and must not import app.db.models;
using duck-typed stand-ins here proves that boundary rather than assuming it.
"""

from dataclasses import dataclass

import pytest

from app.actions.rules import ActionIntent, evaluate_event, evaluate_link

THREAD_ID = "thread-1"
GMAIL_THREAD_ID = "gmail-thread-1"


@dataclass
class FakeRule:
    id: str
    trigger: str
    trigger_params: dict | None
    action_type: str
    action_params: dict | None
    is_deleted: bool = False


@dataclass
class FakeEvent:
    id: str
    status: str
    field: str | None
    new_value: str | None


@dataclass
class FakeLink:
    id: str
    thread_id: str
    gmail_thread_id: str


def _stage_rule(**overrides) -> FakeRule:
    defaults = dict(
        id="rule-1", trigger="entity_entered_stage",
        trigger_params={"stage": "won"}, action_type="archive_thread",
        action_params=None,
    )
    defaults.update(overrides)
    return FakeRule(**defaults)


def _link_rule(**overrides) -> FakeRule:
    defaults = dict(
        id="rule-2", trigger="thread_linked", trigger_params=None,
        action_type="label_thread", action_params={"label": "Tracked"},
    )
    defaults.update(overrides)
    return FakeRule(**defaults)


def _applied_event(**overrides) -> FakeEvent:
    defaults = dict(id="evt-1", status="applied", field="stage", new_value="won")
    defaults.update(overrides)
    return FakeEvent(**defaults)


# ---------------------------------------------------------------------------
# evaluate_event
# ---------------------------------------------------------------------------


def test_matches_entity_entered_stage_on_exact_stage():
    rule = _stage_rule()
    event = _applied_event()
    intents = evaluate_event(event, rules=[rule], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)
    assert intents == [
        ActionIntent(
            rule_id="rule-1", action_type="archive_thread", action_params=None,
            source_event_id="evt-1", source_link_id=None,
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
        )
    ]


def test_no_match_when_new_value_differs_from_trigger_stage():
    rule = _stage_rule(trigger_params={"stage": "lost"})
    event = _applied_event(new_value="won")
    assert evaluate_event(event, rules=[rule], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID) == []


def test_no_match_when_field_is_not_stage():
    rule = _stage_rule()
    event = _applied_event(field="owner", new_value="won")
    assert evaluate_event(event, rules=[rule], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID) == []


def test_no_match_for_thread_linked_rule_against_an_event():
    rule = _link_rule()
    event = _applied_event()
    assert evaluate_event(event, rules=[rule], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID) == []


def test_skips_deleted_rule():
    rule = _stage_rule(is_deleted=True)
    event = _applied_event()
    assert evaluate_event(event, rules=[rule], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID) == []


def test_rule_with_no_trigger_params_never_matches():
    rule = _stage_rule(trigger_params=None)
    event = _applied_event()
    assert evaluate_event(event, rules=[rule], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID) == []


def test_multiple_matching_rules_all_fire():
    rule_a = _stage_rule(id="rule-a")
    rule_b = _stage_rule(id="rule-b", action_type="label_thread", action_params={"label": "Won"})
    event = _applied_event()
    intents = evaluate_event(event, rules=[rule_a, rule_b], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)
    assert {i.rule_id for i in intents} == {"rule-a", "rule-b"}


def test_empty_rules_list_yields_no_intents():
    event = _applied_event()
    assert evaluate_event(event, rules=[], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID) == []


@pytest.mark.parametrize("status", ["pending_review", "rejected", "reverted"])
def test_asserts_event_must_be_applied(status):
    rule = _stage_rule()
    event = _applied_event(status=status)
    with pytest.raises(AssertionError):
        evaluate_event(event, rules=[rule], thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)


# ---------------------------------------------------------------------------
# evaluate_link
# ---------------------------------------------------------------------------


def test_matches_thread_linked_rule():
    rule = _link_rule()
    link = FakeLink(id="link-1", thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)
    intents = evaluate_link(link, rules=[rule])
    assert intents == [
        ActionIntent(
            rule_id="rule-2", action_type="label_thread", action_params={"label": "Tracked"},
            source_event_id=None, source_link_id="link-1",
            thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID,
        )
    ]


def test_no_match_for_entity_entered_stage_rule_against_a_link():
    rule = _stage_rule()
    link = FakeLink(id="link-1", thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)
    assert evaluate_link(link, rules=[rule]) == []


def test_skips_deleted_rule_for_link():
    rule = _link_rule(is_deleted=True)
    link = FakeLink(id="link-1", thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)
    assert evaluate_link(link, rules=[rule]) == []


def test_multiple_matching_rules_all_fire_for_link():
    rule_a = _link_rule(id="rule-a")
    rule_b = _link_rule(id="rule-b", action_type="draft_reply", action_params={"instructions": "say hi"})
    link = FakeLink(id="link-1", thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)
    intents = evaluate_link(link, rules=[rule_a, rule_b])
    assert {i.rule_id for i in intents} == {"rule-a", "rule-b"}


def test_empty_rules_list_yields_no_intents_for_link():
    link = FakeLink(id="link-1", thread_id=THREAD_ID, gmail_thread_id=GMAIL_THREAD_ID)
    assert evaluate_link(link, rules=[]) == []
