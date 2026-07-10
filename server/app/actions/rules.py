"""The pure Phase 5 (actions, spec 006) rule evaluator: which
TaskActionRules, if any, a just-applied TaskEvent or a freshly-created
TaskThreadLink should fire.

Deliberately PURE like task_engine/transitions.py's validator, but one step
further: no IO *and no `app.db.models` import at all* (transitions.py does
import the ORM models; this module does not, so it can be exhaustively unit
tested with plain duck-typed stand-ins and never accidentally grows a
session/DB dependency). Callers pass whatever row objects they have — a real
TaskActionRule/TaskEvent/TaskThreadLink from a live query — as long as they
expose the attributes this module reads (see the Protocols below); it uses
nothing else about them.

The LLM never decides that an action happens — that's what makes this
module deterministic and exhaustively testable. `evaluate_event` and
`evaluate_link` are the two hook points (spec §3): the caller-side helper
that will eventually wire callers to these (`enqueue_action_intents`, a
future task) owns the actual DB writes; this module only decides *whether*
a rule fires and, if so, hands back the frozen params an insert needs.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class RuleLike(Protocol):
    """Duck-typed shape this module reads off a rule row. A real
    TaskActionRule satisfies this; so does any test stand-in with the same
    attributes."""

    id: str
    trigger: str
    trigger_params: dict | None
    action_type: str
    action_params: dict | None
    is_deleted: bool


@runtime_checkable
class EventLike(Protocol):
    """Duck-typed shape this module reads off an event row. Only the
    attributes `evaluate_event` actually inspects — NOT the full TaskEvent
    shape."""

    id: str
    status: str
    field: str | None
    new_value: str | None


@runtime_checkable
class LinkLike(Protocol):
    """Duck-typed shape this module reads off a link row. `gmail_thread_id`
    is NOT a real TaskThreadLink column (the link only carries thread_id) —
    the caller is responsible for handing evaluate_link an object that also
    carries the linked thread's gmail_thread_id (resolved from InboxThread),
    the same way evaluate_event's caller resolves and passes thread_id/
    gmail_thread_id explicitly rather than expecting TaskEvent to carry
    thread-level denormalization it doesn't have either."""

    id: str
    thread_id: str
    gmail_thread_id: str


@dataclass(frozen=True)
class ActionIntent:
    """One rule's request to act, evaluated but not yet persisted. Carries
    exactly the fields a TaskAction insert needs — action_type/action_params
    are copied here (frozen) straight from the rule, so a later rule edit
    never rewrites an already-decided intent's history. Exactly one of
    source_event_id/source_link_id is set, mirroring TaskAction's CHECK
    constraint."""

    rule_id: str
    action_type: str
    action_params: dict | None
    source_event_id: str | None
    source_link_id: str | None
    thread_id: str
    gmail_thread_id: str


def evaluate_event(
    event: EventLike,
    *,
    rules: list[RuleLike],
    thread_id: str,
    gmail_thread_id: str,
) -> list[ActionIntent]:
    """Which of `rules` fire for this APPLIED event.

    Only ever call this with a status='applied' event — pending/rejected/
    reverted events must never reach here (design §3: "Only APPLIED events
    ever reach the evaluator"). Callers filter; this asserts the contract
    rather than silently no-op'ing a misuse.

    A rule fires when: it is not soft-deleted, its trigger is
    'entity_entered_stage', the event's field is 'stage', and the event's
    new_value equals the rule's trigger_params["stage"]. Rules with any
    other trigger (or a deleted rule) are skipped.
    """
    assert event.status == "applied", (
        f"evaluate_event called with a non-applied event (status={event.status!r}); "
        "only APPLIED events may ever reach the rule evaluator"
    )

    intents: list[ActionIntent] = []
    if event.field != "stage":
        return intents

    for rule in rules:
        if rule.is_deleted:
            continue
        if rule.trigger != "entity_entered_stage":
            continue
        trigger_stage = (rule.trigger_params or {}).get("stage")
        if trigger_stage != event.new_value:
            continue
        intents.append(
            ActionIntent(
                rule_id=rule.id,
                action_type=rule.action_type,
                action_params=rule.action_params,
                source_event_id=event.id,
                source_link_id=None,
                thread_id=thread_id,
                gmail_thread_id=gmail_thread_id,
            )
        )
    return intents


def evaluate_link(link: LinkLike, *, rules: list[RuleLike]) -> list[ActionIntent]:
    """Which of `rules` fire for this freshly-created link.

    Only ever call this for a genuinely NEW TaskThreadLink — an
    upsert_link() call that inserted a row, never one that merely updated an
    existing one (design §3: "Only genuinely new links fire (upsert-no-op !=
    new)"). Like evaluate_event's applied-only contract, that freshness
    check is the caller's responsibility; this module has no way to tell an
    insert from an update from the row alone.

    A rule fires when it is not soft-deleted and its trigger is
    'thread_linked' — thread_linked rules carry no trigger_params to further
    match against (spec §2), so every non-deleted thread_linked rule fires
    on every qualifying link.
    """
    intents: list[ActionIntent] = []
    for rule in rules:
        if rule.is_deleted:
            continue
        if rule.trigger != "thread_linked":
            continue
        intents.append(
            ActionIntent(
                rule_id=rule.id,
                action_type=rule.action_type,
                action_params=rule.action_params,
                source_event_id=None,
                source_link_id=link.id,
                thread_id=link.thread_id,
                gmail_thread_id=link.gmail_thread_id,
            )
        )
    return intents
