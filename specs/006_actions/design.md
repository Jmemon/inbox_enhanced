<!-- stamp: 2e6990f (main) | 2026-07-10 | Phase 5 actions design — brainstormed + user-ratified -->

# 006 — Actions

Tasks act on the inbox: deterministic per-task rules fire on applied events
and archive threads, apply labels, or have the LLM write a **draft** reply.
Default-deny capability ladder, full audit chain (email → event → action),
reversible where possible, and **no send capability exists anywhere**.

Origin: 004 chosen-architecture §3 Phase 5 + §1 item 7 (q-d). Decisions
ratified with the user on 2026-07-10:

1. **Trigger = rules + LLM drafts.** Deterministic per-task rules the user
   configures fire on APPLIED events. The LLM never decides that an action
   happens; it only writes draft-reply content when a rule requests it.
   (Rejected: LLM-proposed actions; hybrid channel — deferred.)
2. **Rules ARE the grants.** One table; each rule row carries its own mode
   (`propose`|`auto`); no rule → that action can never happen (default-deny
   by absence). No separate master dial. `draft_reply` is server-capped at
   propose regardless of the row.
3. **Surface = existing review tray + feeds.** Proposed actions join the
   pending queue in ReviewTray/ReviewFeed; executed actions appear in the
   activity feeds with undo where reversible. No new surface.
4. **Scopes: everything at signup; migration for existing accounts.** The
   OAuth `SCOPES` list gains `gmail.modify` + `gmail.compose` for all new
   signups (user has already enabled both on the GCP console consent
   screen). Existing accounts re-consent via a HUD banner → the existing
   OAuth flow → granted scopes recorded. (Rejected: just-in-time per-rule
   consent; settings-page toggle.)

---

## 1. Scopes & account migration

- `server/app/auth/google_oauth.py` `SCOPES` += `https://www.googleapis.com/auth/gmail.modify`,
  `https://www.googleapis.com/auth/gmail.compose`. (GCP consent screen
  already lists both — done by the user 2026-07-10.)
- `users.gmail_granted_scopes` (JSON list, nullable — migration 0011):
  written on **every** OAuth callback from the token response's actual
  `scope` field (space-separated string → list). Never assumed from the
  request. NULL (pre-migration accounts) is treated as readonly-only.
- **Migration banner**: when the authed user's granted scopes lack
  `gmail.modify` or `gmail.compose`, the HUD shows a persistent,
  session-dismissable banner "Re-connect Gmail to enable actions" whose
  button routes through the normal OAuth start URL (full `SCOPES`,
  `prompt=consent`, `include_granted_scopes=true`) and returns to the app;
  the callback updates the column and the banner disappears. No forced
  logout. `GET /api/me`-equivalent surface (whatever the client already
  uses for auth state — extend it) exposes `has_write_scopes: bool`.
- Rules configured before re-consent save normally but are **inert** with a
  "needs permission" badge; they activate automatically once scopes land
  (the evaluator checks scopes at fire time, not save time).

## 2. Data model (migration 0011)

### `task_action_rules` — rules are the grants

| column | type | notes |
|--------|------|-------|
| `id` | String(36) PK | uuid4 hex |
| `task_id` | String(36) FK tasks.id NOT NULL indexed | tracker-kind tasks only (v1) — enforced at the API layer |
| `trigger` | String(32) NOT NULL | `'entity_entered_stage'` \| `'thread_linked'` |
| `trigger_params` | JSON/JSONB nullable | `{"stage": "..."}` for entity_entered_stage; null for thread_linked |
| `action_type` | String(32) NOT NULL | `'archive_thread'` \| `'label_thread'` \| `'draft_reply'` |
| `action_params` | JSON/JSONB nullable | `{"label": "..."}` for label_thread; `{"instructions": "..."}` for draft_reply |
| `mode` | String(16) NOT NULL | `'propose'` \| `'auto'` — server rejects `auto` for `draft_reply` at write time AND caps at execute time (belt + suspenders) |
| `is_deleted` | Boolean NOT NULL default false | soft delete |
| `created_at` | DateTime(timezone=True) NOT NULL | |

### `task_actions` — the audit ledger

| column | type | notes |
|--------|------|-------|
| `id` | String(36) PK | |
| `task_id` | String(36) FK tasks.id NOT NULL indexed | |
| `rule_id` | String(36) FK task_action_rules.id NOT NULL | |
| `source_event_id` | String(36) FK task_events.id nullable | set for `entity_entered_stage` fires |
| `source_link_id` | String(36) FK task_thread_links.id nullable | set for `thread_linked` fires — links are rows in `task_thread_links`, NOT task_events, so the audit chain's middle link is typed: exactly ONE of `source_event_id`/`source_link_id` is non-null (CHECK constraint, valid on both dialects). The 004 mandate "every action carries source_event_id" generalizes to "every action carries its evidence source" — for links, that's the link row (origin llm/user + confidence). |
| `thread_id` | String(36) nullable | soft pointer to inbox_threads |
| `gmail_thread_id` | String(64) NOT NULL | denormalized — audit survives inbox churn (mirrors task_events.gmail_message_id) |
| `action_type` / `action_params` | as on the rule, frozen at creation | rule edits don't rewrite history |
| `status` | String(16) NOT NULL | `'proposed'` \| `'executed'` \| `'rejected'` \| `'undone'` \| `'failed'` |
| `result` | JSON/JSONB nullable | what actually changed, enough to undo: e.g. `{"removed_label_ids": ["INBOX"]}`, `{"added_label_ids": [...]}`, `{"draft_id": "..."}` |
| `error` | Text nullable | populated on `failed` (incl. "needs permission") |
| `created_at` / `executed_at` | DateTime(timezone=True); executed_at nullable | |

- **Idempotency**: two partial unique indexes — `(rule_id, source_event_id)
  WHERE source_event_id IS NOT NULL` and `(rule_id, source_link_id) WHERE
  source_link_id IS NOT NULL` — refold/replay of events and re-upserts of
  links can never double-fire a rule.
- `users.gmail_granted_scopes` also lands in 0011.
- JSON columns use the repo's `with_variant(JSONB, "postgresql")` convention;
  SQLite migration test per-revision as usual.

## 3. Execution engine

- **`server/app/actions/rules.py` — pure evaluator** (no IO, exhaustively
  unit-testable like `transitions.py`): `evaluate(applied_event, rules) ->
  list[ActionIntent]`. `entity_entered_stage` matches an applied event whose
  `field == 'stage'` and `new_value == trigger_params['stage']`;
  `thread_linked` matches link-creation events (pin the exact event shape
  from the current engine when implementing). Only APPLIED events ever reach
  the evaluator — pending/rejected/reverted never fire rules.
- **Hook points**: (a) event-triggered rules — the paths that apply events
  (extraction apply in the worker, approve in the API) call one shared
  `enqueue_action_intents` helper after commit; (b) link-triggered rules —
  the paths that create a FRESH llm/user link (`_write_task_links` in sync/
  extend/backfill, attach in the API) call the same helper with the link as
  source. Only genuinely new links fire (upsert-no-op ≠ new). Intent insert
  is guarded by the unique indexes; conflict → skip silently (replay).
- **Modes**: `propose` → row `proposed`, publishes the review-feed nudge.
  `auto` → row inserted `proposed`→immediately enqueued to a decoupled
  Celery task `execute_action(user_id, action_id)` (never under
  `sync_lock`) which executes and flips to `executed`/`failed`.
  Approving a proposed action calls the same execute path synchronously
  from the API (consistent with corrections being synchronous writes).
- **Gmail write client** (`gmail/client.py` additions): `archive_thread`
  (threads.modify removeLabelIds=['INBOX']), `label_thread` (get-or-create
  label by name, threads.modify addLabelIds), `create_draft` (users.drafts
  .create with a threaded reply: In-Reply-To/References from the source
  message, To = its sender). Every write **preflights
  `users.gmail_granted_scopes` from the DB** — missing scope → action
  `failed` with error "needs permission", never a crash, never an implicit
  consent prompt.
- **draft_reply**: LLM call (Sonnet-class via the existing OpenRouter
  client, new `llm_calls.stage = 'action'`) with inputs: task goal, rule
  `instructions`, thread text from Postgres, the source event's evidence.
  Output = draft body only. Execution creates a Gmail **draft**; nothing is
  ever sent. Server-side cap: `execute_action` refuses to auto-run
  `draft_reply` even if a row says `auto` (and the API rejects writing such
  a rule).
- **Undo** (archive/label only, from the activity feed): replays the exact
  inverse of `result` (re-add removed labels / remove added labels), flips
  status to `undone`. Undo of a `draft_reply` is not offered (delete the
  draft in Gmail if unwanted).
- **Revert interaction**: reverting/refolding away a source event
  auto-rejects its still-`proposed` actions (same transaction); detaching a
  thread does the same for still-`proposed` link-sourced actions. `executed`
  actions are untouched — the audit trail stands; we do not un-archive on
  revert. Approving a proposed action whose source event was reverted (or
  whose source link was detached) → 409.

## 4. API surface

- `POST /api/tasks/{task_id}/rules` / `GET .../rules` / `PATCH .../rules/{rule_id}`
  / `DELETE .../rules/{rule_id}` (soft): tracker-kind tasks only (422 for
  buckets), owner-scoped, `draft_reply`+`auto` → 422 "draft_reply cannot
  auto-run". Rule create/edit/delete bumps `tasks.version` (the SSE
  convergence signal the client already trusts).
- `POST /api/actions/{action_id}/approve` (execute now; 409 wrong status or
  reverted source), `POST /api/actions/{action_id}/reject`,
  `POST /api/actions/{action_id}/undo` (executed + reversible only).
  Owner-scoped via the task join; cross-user scoping is a security boundary
  (same posture as the feeds).
- `/api/reviews` and `/api/activity` items gain `"type": "event"|"action"`;
  action items carry `{action_id, task_id, task_name, action_type,
  action_params, thread subject/display, rule trigger summary, status}`.
  Existing event items keep their shape (additive `type` field only).
- Auth-state surface gains `has_write_scopes: bool`.

## 5. Client

- **Rules section** on the task page (trackers only): list + add/edit modal
  (trigger picker → stage dropdown from the task's schema stages / linked
  trigger → action picker → params → propose/auto toggle, disabled to
  propose for draft_reply) + "needs permission" badge when
  `has_write_scopes` is false.
- **ReviewTray/ReviewFeed**: action cards (task name, rule summary, action +
  target thread, approve/reject) alongside event cards — same busy/timeout
  idiom.
- **Activity ticker/feed**: executed actions render with `[undo]` where
  `status='executed'` and action_type is reversible; undone/failed render
  with their status.
- **Scope banner**: HUD-level, shown when `has_write_scopes` is false,
  session-dismissable, button = existing OAuth start URL.

## 6. Safety invariants (each gets a test)

1. Default-deny by absence: no rule → no action, ever.
2. `draft_reply` can never auto-run: rejected at rule-write AND capped at
   execute.
3. No send: no `gmail.send` scope, no send method, no code path.
4. Every Gmail write preflights granted scopes from the DB; missing →
   `failed` "needs permission".
5. Every action row carries exactly one evidence source (`source_event_id`
   XOR `source_link_id`, CHECK-enforced); the chain email → evidence →
   action is FK-enforced end to end.
6. Idempotent under event replay/refold AND link re-upserts via the two
   partial unique indexes.
7. Reverted source event / detached source link ⇒ pending proposals
   auto-reject; approve-after-revert(or-detach) 409.
8. All new routes owner-scoped; cross-user probes in tests.
9. Buckets cannot have rules (422) — no events means no triggers anyway;
   the guard keeps the invariant explicit.

## 7. Out of scope / deferred

- LLM-suggested actions (the hybrid channel), rule templates.
- Un-archive on event revert.
- `gmail.send` in any form — permanently out per 004.
- Per-bucket rules; multi-action rules; cross-task rules.
- Retry UI for failed actions (dismissable; recreate the rule fire by
  re-approving is not offered in v1).

## 8. Testing invariants

- `actions/rules.py` pure — exhaustive unit tests (each trigger × event
  shape, non-applied statuses never fire).
- Engine: idempotency under replay; auto-execution decoupled from
  sync_lock; failure paths write `failed` + error (reuse the jobs-surface
  guarded-rollback/fresh-session discipline where applicable).
- Gmail client writes: mocked googleapiclient, scope preflight paths.
- Migration 0011 per-revision test.
- Client: build + tsc + reviewer probes on the review-card type split.
- Suite baseline at plan time: 543 passed.
