<!-- stamp: 3021928 (feature/phase2b-task-ui) | 2026-07-07 -->

# Reference Manifest

Master index of the `inbox_enhanced` reference corpus. Each reference doc is a
**dense navigational index** of one subsystem — file paths, key exports/types,
the services it talks to, the routes/tasks it owns, and the data flows across
process boundaries (API ↔ Postgres ↔ Redis ↔ Worker ↔ Google/Anthropic ↔
browser). They are maps, not tutorials.

## The matching contract

Agents (and humans) select docs by matching their task against the **Scope**
column — **not** by guessing from filenames. Bias toward over-inclusion: reading
an extra dense index is cheaper than missing a dependency. The `reference-lookup`
skill (`.claude/skills/reference-lookup/SKILL.md`) automates this routing.

- **File** — path to the reference doc, relative to `reference/`.
- **Stamp** — `<short-sha> (<branch>) | <YYYY-MM-DD>` the doc was last validated
  against code. Matches the top-of-file stamp inside each doc. A doc whose stamp
  lags the current `HEAD` by many commits should be verified against current code
  and re-stamped.
- **Scope** — one-line description of the subsystem the doc covers. This is the
  field you match a task against.

## Index

| File | Stamp | Scope |
|------|-------|-------|
| TASKS_INDEX.md | 3021928 (feature/phase2b-task-ui) | 2026-07-07 | Task engine (Phase 2A + 2B): EPS (Entity-Pipeline Schema) contract (`task_engine/schema.py`), append-only-event CRUD repo (`task_engine/repo.py`, sticky user-link rule, refold-from-events fold, **2B**: `recent_user_events`/`list_pending_events_for_thread`), the pure 8-step extraction validator (`task_engine/transitions.py` — entity resolution via difflib not pg_trgm, correction fences, deferred-entity-mint `PendingCreate`, idempotency SELECT-first + partial-unique-index race backstop, **2B**: `pending_reason`/`proposed_entity` provenance, first-guard-wins), the one-LLM-call extraction orchestrator (`task_engine/engine.py`, **2B**: threads `recent_user_events` into the prompt), goal→draft cache (`task_engine/draft_cache.py`), criteria grammar shared with buckets (`task_engine/criteria.py`, **2B**: `append_example`/`cap_examples`/`EXAMPLE_CAP=30`/`_neutralize_tags` — spec §4.6 learning loop), the `triage_thread`/`extract_transition`(**2B**: float-confidence clamp)/`propose_task` LLM prompts, the decoupled `workers/task_engine_tasks.py` Celery module (process_task_updates/extract_for_thread/propose_task_draft/backfill_task), the full `api/tasks.py` HTTP surface (goal→draft, tracker CRUD, board/events feed, attach/detach **2B**: `add_example` param + detach auto-rejects pending events, approve/reject/revert, manual state edit, entity merge; **2B**: uniform `HTTPException(422, str)` detail contract), `task_draft_ready`/`task_updated`/`task_backfill_progress` SSE events, `TASK_APPLY_CONFIDENCE`/`TASK_LINK_CONFIDENCE` gates, `tasks`/`task_thread_links`/`task_state_entities`/`task_events` Postgres tables (migration 0007; **2B** migration 0008 adds `task_events.pending_reason`/`proposed_entity`). |
| INBOX_SYNC_INDEX.md | d5644bc (feature/phase2a-task-engine) | 2026-07-06 | Three-way inbox sync: Gmail ↔ Postgres ↔ browser. Full sync = reconciling upsert (archives/un-archives, never wipes), partial sync mirrors Gmail archive/soft-delete/unread via widened `historyTypes` and self-heals `is_archived`/`is_deleted` from every live fetch (not just full sync), paginated `history.list` (`nextPageToken`, capped `MAX_HISTORY_PAGES`), extend sync, `gmail_last_history_id` cursor + `HistoryGoneError`, poll/full/extend/reclassify Celery tasks now classifying via `triage()` (Phase 2A — bucket pick + tracker relevance in one LLM call, dual-writing `task_thread_links`; sync/extend/reclassify enqueue `process_task_updates` gated on content-touched/freshly-linked ids only — `(all_ids, content_ids)` split, flag-only touches publish but never extract — see TASKS_INDEX), `sync_lock`/`active_users` gates, `last_sync:{uid}` freshness marker + `GET /api/sync/status`, `user:{uid}` pubsub→SSE push, `GET /api/search` (Postgres FTS/ILIKE), client `idLayer`/`displayLayer` LWW merge + auto-extend + archived-thread eviction + shared `useInboxSearch` (HudPage/InboxPage), shell-level `InboxProvider` (single `useInboxSse` subscription for the authed session). |
| WORKERS_INDEX.md | d5644bc (feature/phase2a-task-engine) | 2026-07-06 | Celery worker + beat: celery_app factory (broker/backend=REDIS_URL, eager test mode), 30s beat tick → enqueue_polls fan-out, tasks (poll_new_messages, full_sync_inbox_task, extend_inbox_history_task, draft_preview_bucket, reclassify_user_inbox), `last_sync:{uid}` freshness marker written on 6 sync-completion sites (read by `GET /api/sync/status`), gmail_sync orchestration (reconciling full sync, widened-historyTypes partial sync) now driven by `_triage_batch` (Phase 2A, replacing `_classify_batch` — bucket + tracker relevance in one call, dual-writes `task_thread_links`; `(all_ids, content_ids)` return split so extraction only sees content-touched threads, extend returns `new_link_ids`), the decoupled `workers/task_engine_tasks` Celery module fed by 6 enqueue-hook sites (incl. extend's freshly-linked enqueue), sync_lock:{uid}, active_users zset gating, preview:{draft_id} cache-before-publish, pubsub user:{uid} SSE fan-out, LLM via OpenRouter (LLM_CLASSIFY_MODEL/LLM_EXTRACT_MODEL/LLM_CONCURRENCY) with per-call llm_calls metrics persistence (app/llm/metrics.py), reclassify/score paths read Postgres bodies (no Gmail refetch). Full task-engine detail → TASKS_INDEX.md. |
| CLIENT_INDEX.md | 3021928 (feature/phase2b-task-ui) | 2026-07-07 | React 19 + Vite browser SPA (client/src): react-router-dom routing (`AppShell` layout route wrapping `<Outlet>` in `state/InboxProvider.tsx` + (**2B**) `state/TasksProvider.tsx`, `/` HudPage, `/inbox` InboxPage, **2B**: `/tasks/:taskId` TaskDetail), App/AuthProvider, lib/api fetch wrappers (+ `searchInbox`, `getSyncStatus`, **2B**: full task-engine fetcher set + `throwWithDetail` 422-detail helper) + lib/sse EventSource singleton (**2B**: `task_draft_ready`/`task_updated`/`task_backfill_progress`), `InboxProvider`/`useInboxStore()` composing useBuckets/useInbox/useInboxSse **once** per authed session (shared store, no per-route remount-refetch; permanent `useInboxSse` subscription pins the SSE singleton; `useInbox`'s `autoExtendEnabled` route-gates auto-extend to `pathname==='/inbox'` so the session-wide instance can't fire at login/on the HUD), **2B**: `TasksProvider`/`useTasksStore()` — the analogous shared task store (task list + per-task detail cache + backfill progress), one SSE subscription refetching on a version-gap **OR** pending-count-gap (version alone is insufficient — several server mutations publish `task_updated` without bumping version), shared debounced (300ms) inbox search with monotonic race guard (`pages/search/`, **2B**: also powers `ThreadsPanel`'s search-to-attach), HudPage sync-freshness strip + store-derived bucket counts + (**2B**) task cards + new-task entry, inbox list/pagination/reload, bucket filter/new/view modals, **2B**: full task-engine UI (`pages/task/`: `NewTaskWizard`+`SchemaEditor` goal→draft→create flow, `TaskDetail` composition root with two-window pending-review events fetch, `PipelineBoard`, `EntityDrawer`, `ReviewFeed`, `ThreadsPanel`); the Browser↔API fetch+SSE realtime layer. |
| CICD_INDEX.md | 695e2f3 (main) | 2026-05-29 | Git pre-commit reference-integrity automation (not HTTP/Celery): .githooks/pre-commit + cicd/scripts/{reference-check,load-env}.sh, scripts/install-hooks.sh symlink into .git/hooks, fuel-code global core.hooksPath forwarder (~/.fuel-code/git-hooks) dispatch + recursion guard, staged-diff vs MANIFEST File/Scope triage via OpenRouter (OpenAI-compatible chat/completions, model anthropic/claude-haiku-4-5), OPENROUTER_API_KEY from gitignored .env, warning-only/fails-open (hook swallows the exit code so --strict never blocks), writes cicd/tasks/update-reference-*.md, doc-stamp contract. |

## Authoring

- New subsystem index → `reference/prompts/CREATE_INDEX.md`
- Extend / split an existing index → `reference/prompts/ADD_REFERENCE.md`

Every doc produced by these prompts MUST carry a top-of-file stamp
`<!-- stamp: <short-sha> (<branch>) | <YYYY-MM-DD> -->` and a matching row here.
If a doc references code that isn't committed yet, commit that code first, then
stamp — an unstamped doc cannot be judged for staleness and is a defect.

The system-wide companion to this corpus is `ARCHITECTURE.md` at the repo root
(processes, inter-process edges, code DAGs, environments, external deps). Use it
as the fallback whenever a needed subsystem index does not exist yet.
