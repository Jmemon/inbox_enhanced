# Later — Scratch: task graphs (stage-sequence → branching DAG, empirically constructed)

> Scratch / idea capture, stamped at commit `84c168a` on branch `main`.
> Targets a version **after** `specs/004_vision_arch/` and the Phase 4.5 jobs
> surface (`plans/2026-07-09-phase4.5-jobs-surface.md`). Not a spec — raw
> thinking, meant to be promoted into a real spec later.

## The sketch (as stated)

A task's state is currently **a sequence of states** — a straight line the entity
walks from start to terminal. That's too flat. Tasks should be allowed a **more
sophisticated representation**, and the way the agent **initially constructs** that
representation should stop being a guess: it should draw on **empirical information
about how the task tends to play out** (from real data) *and* on **what we know
about this user**. And because the "right" shape of a task is genuinely uncertain,
representing it should become a **HITL back-and-forth** — the agent and the user
negotiate how best to model the task before committing to it.

Three moves, in order of dependence:

1. Richer data structure — a **branching DAG** instead of a linear stage list.
2. **Empirical + user-informed construction** — build that DAG from evidence, not a
   template.
3. **HITL negotiation** — grow the existing draft→preview→confirm wizard into a
   multi-turn conversation about the representation.

## What exists today

The `pipeline` field kind (see `agent-1-task-engine.md` §1 and
`server/app/tasks_engine/schema.py`) is:

```jsonc
{"name": "stage", "kind": "pipeline",
 "stages": ["applied", "phone screen", "onsite", "offer"],   // ordered, flat
 "terminal": ["offer", "rejected", "withdrawn"]}
```

An ordered list plus a terminal set — i.e. a **path graph**. The linearity leaks into
the rest of the engine:

- **`transitions.py::validate_and_stage`** encodes a "pipeline regression flag":
  moving *backward in `stages` order* is allowed but forced to `pending_review`.
  "Backward in a list" is the only notion of an illegal/surprising move it has.
- **The board UI** renders one column per stage, left-to-right — a Kanban line.
- **SchemaEditor** (Phase 4.5 T7) edits `stages` as a reorderable chip list with
  `→` arrows and drag — a UI whose entire mental model is "put these in order."

That linear assumption is the thing being generalized. Everything below is an
extension of mechanisms that already exist, not a rewrite.

## The enhancement — branching DAG

Model the pipeline as a **directed acyclic graph** of states:

- **Nodes** = states (the current `stages` + `terminal` members).
- **Edges** = *allowed* transitions. Forks (one state → several possible next
  states), merges (several states → one), and **multiple distinct paths to a
  terminal**.
- A linear `stages` list is exactly the **degenerate case** — a path graph — so this
  is backward-compatible: existing tasks are DAGs that happen to have one edge out of
  each node.

What this buys us over a line:

- Real pipelines fork. A job hunt goes `applied → screen`, but from `screen` it can
  branch to `take-home`, or `onsite`, or straight to `rejected`. An apartment search
  forks on `applied → {approved, waitlisted, denied}`. A line can't say "these two
  next-states are both legitimate."
- **"Regression" stops being about list order.** The engine's illegal-move signal
  generalizes from "moved backward in the array" to **"this transition is not an edge
  in the graph"** → route to `pending_review`. Cleaner, and correct for graphs where
  "backward" is undefined.

Deliberately **out of scope** (keep v1 of this idea tight): concurrent/parallel
sub-states, nested sub-tasks, and weighted/probabilistic edges. Empirical evidence
shapes **which branches exist** at construction time; it does **not** (yet) get
persisted as per-edge weights. Probabilities/expected-durations are noted as a
possible future layer, not part of this.

## Empirical + user-informed construction

Today the agent proposes a schema from the goal string alone (one LLM call,
`prompts/propose_task.py`) — essentially a template guess. Make construction
**evidence-grounded**, in two layers:

1. **Model / world priors (skeleton).** The LLM's general knowledge of how a goal of
   this *type* usually flows gives the initial graph — the same call we make today,
   but emitting a graph rather than a list.
2. **The user's own corpus (fit to reality).** Mine this user's historical threads
   for how the task *actually* played out for **them**, and use that evidence to
   **add / prune / reorder** branches. Their real job hunt had a take-home round and
   never an "onsite"? The graph should reflect that. This reuses the machinery
   backfill already has — the `_score_all` / relevance pass over existing
   `inbox_threads` (`agent-1-task-engine.md` §3, Phase 4.5 `backfill_task`) — so we're
   scoring history we already scan, not adding a new corpus scan from scratch.

The layering principle: **priors propose, corpus disposes.** The skeleton keeps the
graph sensible even for a user with no history; the corpus makes it *theirs* when
history exists. "User information" also includes anything already known about the
user (other active tasks, prior corrections/criteria examples) — the construction
prompt can condition on it.

**Cross-user aggregate priors** ("how does *everyone's* job hunt flow") are the
obvious stronger source of empirical data and are **explicitly deferred** — privacy-
sensitive, and it changes the data model (shared, anonymized graph stats). Open
question, not this pass.

## HITL negotiation — via the extended wizard

The right shape of a task is uncertain enough that the agent should **check its work
with the user** rather than commit a guess. Grow the **existing** draft→preview→
confirm loop (Phase 4.5 jobs surface) into a **multi-turn negotiation**:

- Agent proposes a DAG → user reviews it in the wizard → user pushes back ("onsite
  can skip straight to offer", "there's no phone-screen stage for me", "split
  'interviewing' into rounds") → agent **revises the graph** → repeat until the user
  confirms.
- The scaffolding is already there: the jobs surface holds the evolving draft in
  **`jobs.payload`** and already models an async `draft_ready` → user-review →
  `confirm` step with a blue-dot review affordance. The extension is making
  `draft_ready` **loopable** (a re-propose round that folds the user's feedback back
  into the payload) instead of a single one-shot review.
- **SchemaEditor** (which just gained arrows + drag for linear order in Phase 4.5)
  grows into a small **graph editor** — add/remove edges, not just reorder a list.
- This is a natural fit with the conversational-ingress idea in
  [`scratch.md`](./scratch.md): once the negotiation is multi-turn, "talk to the HUD"
  could *be* the channel for it — but that's the Intent Gateway's problem, and the
  wizard is the first surface.

## Data-structure & code implications

- **`state_schema` pipeline kind** gains edges — an adjacency/edge-list alongside (or
  replacing) the flat `stages` array. Terminal set stays. Shape TBD (see open
  questions); must stay JSONB and must keep the validator + board mechanical.
- **`schema.py::validate_schema`** gains DAG validation: acyclicity, every node
  reachable from a start, at least one terminal reachable, edges reference real nodes.
  Same "reject anything outside the vocabulary" contract as today.
- **`transitions.py`** — the regression rule generalizes from "backward in `stages`
  order" to "transition ∉ edge set" → `pending_review`. The confidence gate,
  idempotency, entity resolution, no-op filter all stay.
- **SchemaEditor** → graph editor (edges, not just order).
- **Board / HUD rendering** — one-column-per-stage assumes a line; a non-linear graph
  needs a layout (layered/DAG layout, or swimlanes). This is the biggest UI unknown.
- **Migration / backward-compat** — existing linear pipelines are valid path-DAGs;
  ideally zero data migration (read a flat `stages` list *as* a path graph).

## Open questions

- **JSONB representation.** Adjacency list vs. explicit edge list vs. keep `stages`
  and add an `edges` array — which keeps `validate_schema`, `transitions.py`, and the
  board renderer all mechanical and simple? Whatever we pick, a bare `stages` list
  must still read as a legal (linear) graph.
- **Do edges carry guard conditions?** Kept minimal for now — branching *structure*,
  not conditional logic or weights. Revisit if the LLM needs "this edge only when X."
- **Board layout for a non-linear graph.** Columns break down. Layered DAG layout?
  Swimlanes per branch? A different view entirely? Unresolved and load-bearing for UX.
- **Corpus-mining cost/latency at draft time.** How much history to scan, and can the
  construction pass genuinely piggyback on backfill's existing scoring rather than
  adding a separate LLM sweep? Cost/latency budget for the draft step.
- **Multi-turn wizard state.** Where the evolving draft lives (`jobs.payload`), how
  many rounds are reasonable, when/how the agent decides it's converged vs. keeps
  asking, and how a `draft_ready` loop coexists with the current single-shot confirm.
- **Migration** for existing linear pipelines (aim: none — read them as path graphs).
- **Cross-user aggregate priors** — deferred; revisit as a privacy + data-model
  question if per-user corpus + model priors prove too thin.
