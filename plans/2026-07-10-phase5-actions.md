# Phase 5 — Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Spec: `specs/006_actions/design.md`.
> Plan stamped at commit `84c168a` on branch `main`.

**Goal:** Tasks act on the inbox through user-configured rules that fire on applied events / fresh links: archive threads, apply labels (auto-eligible, undoable), or LLM-draft replies (propose-only, never send) — behind a default-deny grant model, full FK'd audit chain, and a scope-migration banner for pre-existing accounts.

**Architecture:** A pure `actions/rules.py` evaluator (the `transitions.py` of this phase) turns (evidence, rules) into intents; two partial unique indexes make firing idempotent under replay; a decoupled `execute_action` Celery task owns Gmail writes with DB scope preflight, reusing the jobs-surface failure discipline. Proposed actions ride the EXISTING review tray/feeds via a `type` discriminator; executed ones ride the activity feeds with undo. New signups get all scopes (GCP consent screen already updated); existing accounts re-consent via a HUD banner through the unchanged OAuth flow.

**Tech Stack:** existing (uv / bun; googleapiclient already present; no new dependencies).

## Global Constraints

- All prior conventions hold: `uv`/`bun` only; NEVER read `.env`; repo functions never commit (write helpers may flush); publish-after-commit; TDD server-side (suite baseline **543 passed**); client verification = `bun run build` + `bun x tsc --noEmit`; worktrees lack `.env` → settings assertions via resolved `get_settings()`; SQLite migration tests → dialect-guard PG-only DDL; JSON `with_variant(JSONB, "postgresql")`; bare `pytest -q` works (conftest sets eager mode).
- **Safety invariants (spec §6) are the phase's acceptance bar** — each of the nine gets at least one test; reviewers treat them as the constraints block.
- `draft_reply` can NEVER auto-run (422 at rule write AND cap at execute — belt + suspenders). No `gmail.send` scope, method, or code path may exist.
- Every Gmail write preflights `users.gmail_granted_scopes` FROM THE DB; missing scope → action `failed` with error "needs permission", never an exception to the caller.
- Cross-user scoping on every new route is a security boundary — probe in tests and review.
- Exactly one of `source_event_id`/`source_link_id` per action row (CHECK), idempotency via the two partial unique indexes.
- Commit per task, `type(scope): summary`, no attribution lines.

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `server/migrations/versions/0011_actions.py` | create | `task_action_rules`, `task_actions`, `users.gmail_granted_scopes` |
| `server/app/db/models.py` | modify | `TaskActionRule`, `TaskAction`, `User.gmail_granted_scopes` |
| `server/app/actions/rules.py` | create | PURE evaluator (no IO) |
| `server/app/actions/repo.py` | create | rules + actions CRUD (never commits) |
| `server/app/auth/google_oauth.py` + `server/app/api/auth.py` | modify | scopes list; capture granted scopes on callback; `/auth/me` gains `has_write_scopes` |
| `server/app/gmail/client.py` | modify | `archive_thread`, `label_thread`, `create_draft` (+ scope preflight helper) |
| `server/app/task_engine/repo.py` | modify | `upsert_link` returns freshness; revert/detach auto-reject hooks |
| `server/app/workers/action_tasks.py` | create | `execute_action` Celery task (+ draft_reply LLM call) |
| `server/app/workers/task_engine_tasks.py` + `gmail_sync.py` | modify | intent hooks at apply/link sites |
| `server/app/api/actions.py` | create | rules CRUD + approve/reject/undo |
| `server/app/api/tasks.py` | modify | approve/attach hooks; feeds `type` discriminator |
| `server/app/llm/prompts/draft_reply.py` | create | draft-body prompt |
| `client/src/lib/api.ts` + pages | modify | types/fetchers; rules UI; action cards; undo; banner |
| `reference/*` | modify (last) | re-index + stamps |

Sequencing: **T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8** (strictly sequential: shared models/migration → auth+gmail plumbing → engine → API → client data+rules UI → client cards/banner → docs → manual gate).

---

### Task 1: Storage + the pure evaluator

**Files:** Create `server/migrations/versions/0011_actions.py`, `server/app/actions/__init__.py`, `server/app/actions/rules.py`, `server/app/actions/repo.py`; Modify `server/app/db/models.py`; Test: create `server/tests/test_migration_0011.py` (per-revision convention), `server/tests/test_action_rules.py` (evaluator), `server/tests/test_actions_repo.py`.

- Models per spec §2 exactly (column lists, types, nullability). `TaskAction`: CHECK constraint `(source_event_id IS NULL) != (source_link_id IS NULL)` (works on both dialects via `sa.CheckConstraint`); two partial unique indexes (`postgresql_where=`/`sqlite_where=` per the 0007 idempotency-index precedent — find and mirror it).
- `actions/rules.py` — PURE, no IO: `ActionIntent` dataclass `(rule_id, action_type, action_params, source_event_id|None, source_link_id|None, thread_id, gmail_thread_id)`; `evaluate_event(event, *, rules, thread_id, gmail_thread_id) -> list[ActionIntent]` — matches `entity_entered_stage` rules where `event.field == "stage"` and `event.new_value == trigger_params["stage"]`; ONLY status-applied events may be passed (assert/document — callers filter); `evaluate_link(link, *, rules) -> list[ActionIntent]` — matches `thread_linked` rules. Both skip `is_deleted` rules.
- `actions/repo.py` (never commits): `create_rule`, `list_rules(db, *, task_id, include_deleted=False)`, `get_owned_rule` (via task join), `soft_delete_rule`, `update_rule`; `insert_intent(db, *, user...) -> TaskAction | None` (returns None on unique-index conflict — SELECT-first + IntegrityError backstop, mirror the 2A idempotency pattern in `task_engine/repo.py`), `get_owned_action` (task join), `set_status`, `list_pending_actions_for_user` / `list_recent_actions_for_user` (Task-joined, user-scoped, `Task.kind == "tracker"` defense-in-depth, newest first — mirror the feeds helpers).
- [ ] TDD: migration up/down; CHECK rejects both-null and both-set; partial unique indexes enforce (insert same (rule, event) twice → second blocked); evaluator exhaustive — each trigger × matching/non-matching params × wrong-field events × deleted rules; repo cross-user probes + conflict-returns-None. Full suite green.
- [ ] Commit: `feat(actions): storage + pure rule evaluator — migration 0011`

---

### Task 2: Scopes, granted-scope capture, Gmail write client

**Files:** Modify `server/app/auth/google_oauth.py`, `server/app/api/auth.py`, `server/app/gmail/client.py`; Test extend `server/tests/test_auth_router.py` (or the auth tests' actual home — read first), create `server/tests/test_gmail_writes.py`.

- `SCOPES` += `https://www.googleapis.com/auth/gmail.modify`, `https://www.googleapis.com/auth/gmail.compose` (GCP consent screen already updated by the user).
- Callback capture: the token-exchange result exposes granted scopes (`creds.scopes` / the token response's `scope` string — read `exchange_code` and thread it through its return dataclass); the callback writes `user.gmail_granted_scopes = <list>` on EVERY login/re-consent. Never write the requested list — only what Google returned.
- `/auth/me` response gains `has_write_scopes: bool` — true iff granted scopes (NULL → readonly-only → false) include BOTH modify and compose. (One flag, not two: the banner story is all-or-nothing since new signups grant both.)
- `gmail/client.py`: module-level scope-preflight helper `require_scopes(user, *needed) -> str | None` (returns an error string or None; NO exception); `archive_thread(db, user, gmail_thread_id)` → `users.threads.modify` remove `INBOX`; `label_thread(db, user, gmail_thread_id, label_name)` → get-or-create label by name (list, case-insensitive match, else create), add id — returns `{added_label_ids, label_id}`; `create_draft(db, user, gmail_thread_id, body_text)` → fetch the thread's latest non-self message for To/In-Reply-To/References/Subject (Re: prefix), `users.drafts.create` with `threadId` — returns `{draft_id}`. All three return result dicts the audit ledger stores; all preflight scopes first.
- [ ] TDD: callback stores exactly Google's returned scopes; NULL column → `has_write_scopes` false; mocked googleapiclient — archive/label/draft happy paths + label get-or-create both branches + preflight-missing returns the error string without touching the API mock. Full suite green (NOTE: no `.env` in worktrees; auth tests use conftest's env seams).
- [ ] Commit: `feat(auth,gmail): full scopes at signup + granted-scope capture + write client`

---

### Task 3: Engine wiring — intents, hooks, execute_action

**Files:** Create `server/app/workers/action_tasks.py`, `server/app/llm/prompts/draft_reply.py`; Modify `server/app/task_engine/repo.py` (`upsert_link` freshness + revert/detach auto-reject), `server/app/workers/task_engine_tasks.py`, `server/app/workers/gmail_sync.py`, `server/app/api/tasks.py` (approve + attach hooks only — routes are T4); Test extend `server/tests/test_task_engine_tasks.py`, `test_task_repo.py`, create `server/tests/test_action_tasks.py`.

- **`upsert_link` contract change**: returns `LinkUpsert(link, newly_attached: bool)` NamedTuple — `newly_attached` true iff the row was inserted OR transitioned into `state='attached'` from absent/detached; a confidence/origin refresh of an already-attached link is false; the sticky no-op returns `(None, False)`. Update ALL call sites (`api/tasks.py:529,575`, `task_engine_tasks.py:~700`, `gmail_sync.py:~151` — grep for others) and their tests.
- **Shared helper** (in `actions/` or `workers/action_tasks.py` — implementer's call, no cycles): `fire_rules_for_event(db, *, task, event, thread_id, gmail_thread_id)` and `fire_rules_for_link(db, *, task, link)` — load active rules for the task, evaluate, `insert_intent` each (None on replay-conflict → skip), and for each inserted intent: `mode='propose'` → leave `proposed` + publish the review nudge (reuse `_publish_task_updated`'s pending-count semantics? NO — actions aren't events; publish `task_updated` with unchanged version/pending is insufficient. Decision: actions get their own SSE nudge `action_updated {task_id}` — client refetches reviews/activity on it, mirroring `job_updated`'s pure-nudge pattern); `mode='auto'` (never draft_reply — assert) → enqueue `execute_action.apply_async(args=[user_id, action.id])` after commit.
- **Hook sites** (call the helper AFTER the surrounding commit, publish-after-commit): (a) extraction apply in `transitions.py:394`'s caller path — hook where the worker commits applied events (find the commit site in `task_engine_tasks.py`, not inside the pure validator); (b) API approve (`api/tasks.py:674` region) after its commit; (c) fresh links: `gmail_sync.py` triage dual-write, `task_engine_tasks.py` backfill link writes, API attach — all gated on `newly_attached`.
- **`execute_action(user_id, action_id)`** in `workers/action_tasks.py`: load action (owner-scoped), 409-style no-op unless status `proposed`; re-check source validity (event not reverted / link still attached) → else `rejected`; dispatch by action_type to the T2 Gmail methods; `draft_reply` first calls the LLM (OpenRouter client, `stage="action"` in llm_calls, prompt in `llm/prompts/draft_reply.py`: task goal + rule instructions + thread text from Postgres + evidence quote → plain body text) then `create_draft`; success → `status='executed'`, `result`, `executed_at`, commit, publish `action_updated`; scope-preflight failure → `failed` + "needs permission"; exception → guarded-rollback + fresh-session `mark failed` + re-raise (REUSE the jobs-surface `_record_job_failure` discipline — mirror, don't import across module seams if it creates cycles).
- **Revert/detach auto-reject**: in `task_engine/repo.py` — `revert_event`/refold path and `detach` path auto-reject still-`proposed` actions whose source is that event/link (same transaction; find the exact revert function names by reading the file).
- [ ] TDD: intent inserted once under event replay (idempotency); propose leaves proposed + publishes `action_updated`; auto enqueues (captured apply_async); execute happy paths per action_type (mocked Gmail + mocked LLM for draft); needs-permission → failed; reverted-source → rejected at execute; detach auto-rejects proposed link-sourced actions; revert auto-rejects event-sourced; `newly_attached` semantics (fresh/re-attach true; refresh false; sticky (None,False)). Full suite green.
- [ ] Commit: `feat(engine): rule firing hooks + execute_action worker`

---

### Task 4: API surface

**Files:** Create `server/app/api/actions.py` (mount in `main.py`); Modify `server/app/api/tasks.py` (feeds discriminator); Test create `server/tests/test_actions_api.py`, extend `server/tests/test_tasks_api.py` (feeds).

- Rules CRUD under the tasks router or the new module (implementer's call, one place): `POST /api/tasks/{task_id}/rules` (422 for bucket-kind task; 422 `"draft_reply cannot auto-run"` for that combo; validates trigger/action vocab + params shapes), `GET .../rules`, `PATCH .../rules/{rule_id}` (mode/params/trigger edits, same validations), `DELETE .../rules/{rule_id}` (soft, idempotent like task delete). Every rule mutation bumps `tasks.version` + publishes `task_updated` (client convergence for the rules section).
- Action routes: `POST /api/actions/{action_id}/approve` → synchronous execute (share the execute path with the worker — one function, two entry points), 409 `"action is not pending"` on wrong status, 409 on reverted/detached source; `POST .../reject` (proposed only, 409 otherwise); `POST .../undo` — only `executed` + reversible types, replays the inverse of `result` via the Gmail client, flips `undone`, 409 otherwise. All owner-scoped via task join, 404 not-owned.
- Feeds: `/api/reviews` merges `list_pending_actions_for_user` with the existing pending events, `/api/activity` merges recent actions — every item gains `"type": "event"|"action"`; action items: `{type, action_id, task_id, task_name, action_type, action_params, status, thread display (subject via thread_id lookup, batched — no N+1), rule trigger summary string, created_at}`. Existing event items unchanged plus `type`. Sort merged lists by created_at desc; limits apply post-merge.
- [ ] TDD: full route matrix incl. 409s, bucket-422, draft_reply-auto-422, undo inverse call shapes (mocked Gmail), cross-user probes on EVERY route, feeds mixed-type ordering + envelope shapes + version bump on rule mutations. Full suite green.
- [ ] Commit: `feat(api): rules CRUD + action approve/reject/undo + typed feeds`

---

### Task 5: Client data layer + rules UI

**Files:** Modify `client/src/lib/api.ts`, `client/src/lib/sse.ts`, `client/src/pages/task/TaskDetail.tsx`; Create `client/src/pages/task/RulesSection.tsx`.

- `api.ts`: `ActionRule`, `TaskAction`, `FeedItem` becomes a discriminated union on `type` (`EventFeedItem | ActionFeedItem` — check every existing consumer compiles: ReviewTray/ReviewFeed/ActivityTicker narrow on `type === 'event'` for now, T6 renders the rest); fetchers `createRule/listRules/patchRule/deleteRule`, `approveAction/rejectAction/undoAction`; `Me` type gains `has_write_scopes`. `sse.ts`: add `{event: 'action_updated'; task_id: string}`.
- `RulesSection.tsx` on TaskDetail (tracker-kind only): rule list (trigger summary → action summary → mode chip → delete) + add/edit inline form: trigger picker (`entity entered stage` w/ stage dropdown from the task's schema `all_stages()` client-side equivalent — the detail payload carries state_schema; `thread linked`), action picker (`archive` / `label` + name input / `draft reply` + instructions textarea), mode toggle (propose/auto; locked to propose + explanatory hint for draft_reply), "needs permission" badge on every rule when `me.has_write_scopes` is false. Converges via the existing task_updated → detail refetch (version bumps on rule mutations).
- [ ] Build + tsc clean. Commit: `feat(client): action rule types + rules section on task page`

---

### Task 6: Client action cards, undo, scope banner

**Files:** Modify `client/src/pages/hud/ReviewTray.tsx`, `client/src/pages/task/ReviewFeed.tsx`, `client/src/pages/hud/ActivityTicker.tsx` (+ the task-page activity list if separate — check), `client/src/pages/hud/HudPage.tsx` (banner), `client/src/state/TasksProvider.tsx` or the feeds' refetch signals (wire `action_updated`).
 
- Action cards in tray + feed: rule summary, action + params, target thread subject, Approve/Reject via the new fetchers with the established per-item busy + 10s timeout idiom. Refetch signals: subscribe the tray/feed refetch to `action_updated` (pure nudge — mirror how `job_updated` is consumed) IN ADDITION to the existing sums.
- Activity: `type==='action'` items render `{task_name}: {action summary} · {ago}` with `[undo]` when `status==='executed'` and type reversible; undone/failed render status text.
- Scope banner: HUD-level (above the sync strip), shown when authed `me.has_write_scopes === false`, session-dismissable (useState, not persisted), button href = the existing OAuth start URL (`/auth/login` — confirm the actual path from `useAuth`/Login.tsx).
- [ ] Build + tsc clean; self-review the union narrowing (no `as` casts) and the new refetch signal for loops. Commit: `feat(client): action review cards + undo + scope migration banner`

---

### Task 7: Reference docs

- [ ] Update `TASKS_INDEX.md` (actions domain: tables/evaluator/repo/routes/feed discriminator/SSE event), `WORKERS_INDEX.md` (action_tasks module, hook sites, execute discipline), `GMAIL`-relevant index (write methods + preflight — INBOX_SYNC_INDEX covers the gmail client; put it where the corpus routes it), `CLIENT_INDEX.md` (RulesSection, action cards, banner, union FeedItem), `AUTH`-relevant coverage (scopes + granted-scope capture — no AUTH_INDEX exists; note it in MANIFEST or add the section where auth is currently indexed), `MANIFEST.md` stamps/scopes. Every claim verified against code; stamp to the final code commit. Commit: `docs(reference): re-index for phase 5 actions`
- [ ] Final: suite green; build clean.

---

### Task 8 (manual acceptance — coordinator + user)

Dev stack (migration 0011 runs; NOTE: the user's existing account predates the scope change): banner shows → re-consent through Google → banner gone, `has_write_scopes` true; add a rule "entity enters stage X → archive (propose)" → move an entity into X → proposal appears in the tray → approve → thread archived in Gmail → undo → back in inbox; flip the rule to auto → next fire executes without asking, visible in activity with undo; add a `thread_linked → label` auto rule → new matching mail gets labeled in Gmail; add a draft_reply rule (locked to propose) → approve → draft appears in Gmail drafts, correctly threaded, never sent; verify a bucket task rejects rule creation; delete a rule and confirm no further fires.
