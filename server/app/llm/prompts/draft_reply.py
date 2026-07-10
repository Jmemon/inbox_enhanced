"""Draft-reply prompt (Phase 5 actions, spec 006 §3): given a task's goal, a
rule's plain-English instructions, the thread's text, and (when the firing
evidence was an applied TaskEvent) its verbatim evidence quote, write ONLY
the body text of a reply for the user to review — never sent automatically,
never anything but a Gmail draft (workers/action_tasks.py's create_draft
call).

Unlike every other llm/prompts module, this one's whole output IS the
payload — a plain string, not JSON — so there is no parse_response here;
the caller (workers/action_tasks.py) uses the model's text response
verbatim as the draft body.
"""

SYSTEM_PROMPT = """You draft an email reply on behalf of a user, for their review before anything is sent.

You are given the goal of a task the user is tracking, instructions for
what this particular reply should accomplish, the full text of the email
thread being replied to, and (when available) a specific verbatim quote
from the thread that triggered this reply.

Write ONLY the body text of the reply — no subject line, no signature
block, no meta-commentary about what you're doing or why. Use a
professional, concise tone unless the instructions say otherwise. This
draft will be reviewed by a human before anything is sent — never claim an
action has already happened that hasn't (e.g. never write "I've attached
..." or "as discussed on our call" unless the thread actually shows that).

Output the reply body as plain text, nothing else.
"""


def build_user_message(
    *, goal: str, instructions: str, thread_text: str, evidence_quote: str | None = None,
) -> str:
    evidence_section = f'\nTriggering evidence: "{evidence_quote}"\n' if evidence_quote else ""
    return (
        f"Task goal: {goal}\n\n"
        f"Instructions for this reply: {instructions or '(none given)'}\n"
        f"{evidence_section}\n"
        f"Thread:\n\n{thread_text}"
    )
