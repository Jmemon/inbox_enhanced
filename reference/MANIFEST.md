<!-- stamp: 13a07e5 (main) | 2026-05-29 -->

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
| INBOX_SYNC_INDEX.md | 13a07e5 (main) | 2026-05-29 | Three-way inbox sync: Gmail ↔ Postgres ↔ browser. Full/partial/extend sync engine, `gmail_last_history_id` cursor + `HistoryGoneError`, poll/full/extend/reclassify Celery tasks, `sync_lock`/`active_users` gates, `user:{uid}` pubsub→SSE push, client `idLayer`/`displayLayer` LWW merge + auto-extend. |
| WORKERS_INDEX.md | cebb1a6 (main) | 2026-05-29 | Celery worker + beat: celery_app factory (broker/backend=REDIS_URL, eager test mode), 30s beat tick → enqueue_polls fan-out, tasks (poll_new_messages, full_sync_inbox_task, extend_inbox_history_task, draft_preview_bucket, reclassify_user_inbox), gmail_sync orchestration, sync_lock:{uid}, active_users zset gating, preview:{draft_id} cache-before-publish, pubsub user:{uid} SSE fan-out, LLM via OpenRouter (LLM_CLASSIFY_MODEL/LLM_CONCURRENCY). |
| CLIENT_INDEX.md | 13a07e5 (main) | 2026-05-29 | React 19 + Vite browser SPA (client/src): App/AuthProvider routing, lib/api fetch wrappers + lib/sse EventSource singleton, useInbox/useInboxSse/useBuckets hooks, inbox list/pagination/reload, bucket filter/new/view modals; the Browser↔API fetch+SSE realtime layer. |
| CICD_INDEX.md | 323bf5a (main) | 2026-05-29 | Git pre-commit reference-integrity automation (not HTTP/Celery): .githooks/pre-commit + cicd-scripts/{reference-check,load-env}.sh, scripts/install-hooks.sh symlink into .git/hooks, fuel-code global core.hooksPath forwarder (~/.fuel-code/git-hooks) dispatch + recursion guard, staged-diff vs MANIFEST File/Scope triage via Anthropic Haiku (claude-haiku-4-5), ANTHROPIC_API_KEY from gitignored .env, warning-only/fails-open (hook swallows the exit code so --strict never blocks), writes tasks/update-reference-*.md, doc-stamp contract. |

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
