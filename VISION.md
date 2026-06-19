# VISION

> Stamped at commit `6fbb58d` on branch `main`.

## What this is becoming

This is not an email client. The destination is a **HUD**: a surface where the user
explores their data the way an analyst does EDA, and creates **tasks** that run
against it. The inbox is a view you can navigate to — useful for spot-checking and
for seeing how up-to-date the system is — but it is not the product. The product is
the layer above it.

The HUD concept is data-source-agnostic. The scope of *this repo* is its first data
source: the user's Gmail inbox, kept live.

## Tasks

A **task** is the central concept. Creating one means defining two things, with a
third emerging from them:

1. **Relevance** — which incoming emails belong to this task. This is LLM
   classification against user-defined criteria, the same mechanism the bucket
   classifier already proves out.
2. **State representation** — what the task tracks, derived from the relevant
   emails. State is task-specific, not a fixed schema.
3. **Action** (eventually) — tasks don't just track; they can act on the user's
   behalf. There is no separate "automations" concept: acting is a capability of
   tasks.

The toy example: **find a job**. Emails from recruiters and companies are classified
as relevant; the task's state is a per-company pipeline (applied → Nth interview →
offer / rejected), updated as emails arrive. Same shape applies to *find an
apartment*, *find a therapist* — any sustained effort whose progress is legible from
the inbox.

**Buckets are the degenerate task**: classify-only, no state, no actions. The
existing classifier isn't a separate feature to maintain alongside tasks — it is v1
of the task engine, and the vision unifies everything under the task concept.

## The data layer

Tasks are only as good as the data under them. Requirements:

- **Backend ↔ Gmail**: the backend's copy of the inbox stays tightly in sync with
  Gmail — faster and more reliably than the current periodic poll.
- **Frontend ↔ backend**: the UI reflects backend state live (new emails, task
  state changes) without manual reloads.
- **Searchable storage**: inbox storage must be quick to search, because the HUD's
  exploration loop (EDA over your own inbox) depends on it.

## LLM observability

Tasks are LLM-driven end to end — relevance classification and state-transition
extraction are both model calls, and cost/latency scale with inbox volume. Every LLM
call in the system must therefore be instrumented and the metrics persisted, not just
logged. At minimum, per call:

- **Tokens** — input, output, and (for Anthropic prompt caching) cache-creation and
  cache-read input tokens, since they price differently.
- **Cost** — derived from token counts × the model's pricing, recorded per call so it
  rolls up per task, per user, and system-wide.
- **TTFT** — time to first token.
- **Throughput** — output tokens/sec.
- **Latency** — total wall-clock for the call.
- **Context** — model id, which prompt/pipeline stage (classify vs. extract vs.
  draft-preview), task id, and outcome (success / retry / error).

This makes the unit economics of a task legible — "what does keeping this tracker live
cost per day?" — and surfaces regressions in model performance over time. *How* the
metrics are stored, aggregated, and surfaced is left to the implementing plan.

## Open questions

Deliberately unresolved; settling them is spec work, not vision work.

- **How does a user define a task's state representation?** "Find a job" implies a
  per-company status pipeline — but how does a user (or the LLM, guided by the
  user) specify that schema when creating a task? Fully user-authored, LLM-proposed
  from a natural-language goal, or picked from templates?
- **How do emails update task state?** Classification says an email is relevant;
  something must then map it to a state transition (e.g. "this email means company
  X moved to onsite"). What does that processing step look like, and how is it
  validated?
- **Manual correction** — users will need to manually attach/detach emails to a
  task and fix wrong state transitions. How much of the UI is built around this
  feedback loop?
- **What actions are tasks allowed to take**, and what is the consent model when a
  task acts on the user's behalf?
