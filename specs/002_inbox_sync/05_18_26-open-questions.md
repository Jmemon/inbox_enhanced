# Open questions / undefined areas

Gaps in the current `002_inbox_sync` spec, surfaced by comparing `flows.md`, `storage.md`, `inbox-ops.md`, and `inbox-op-triggers.md` against `ARCHITECTURE.md`. Goal of the spec: refine state sync across `gmail ⇄ backend` and `backend ⇄ frontend`. Each item below is something the spec currently does not pin down.

---

## 1. gmail ⇄ backend

### 1.1 Label / archive / delete propagation
Current worker code uses `historyTypes=messageAdded` only. If the user archives, deletes, or relabels in Gmail, the backend silently drifts.

Decisions needed:
- Mirror `INBOX` label removal (archive) — yes/no?
- Mirror `messageDeleted` — yes/no?
- Track read/unread, starred, other labels — yes/no?
- `check_for_inbox_drift` currently only detects "missing from backend". Needs a path for "in backend, missing from gmail" (i.e. deletions detectable by the drift sweep even when history is intact).

### 1.2 Body + attachment storage
`storage.md` lists `## Postgres` and `## Blob` as empty sections. Today only `body_preview` lives in postgres.

Decisions needed:
- Blob backend choice (S3 / Supabase Storage / Railway volume).
- Key schema (e.g. `{user_id}/{thread_id}/{message_id}/...`).
- What gets stored (raw RFC822? parsed text + HTML? individual MIME parts? inline images?).
- Attachment size cap and accepted MIME types.
- Retention / deletion policy when a thread is removed.
- Access pattern: signed URL vs auth-gated proxy endpoint.
- Encryption at rest.
- Postgres-side tracking table for blob refs (mentioned but not modelled).

### 1.3 Attachment fetch timing
Eager (during message ingest) vs lazy (on first view). `users.messages.attachments` is per-part and has real quota cost.

### 1.4 Inactive-user hourly poll
Mentioned in `flows.md` but undefined.

Decisions needed:
- Implementation surface: new celery beat entry?
- "Inactive" definition for this purpose vs the `active_users` zset.
- Sharding strategy across the hour (thundering herd risk against Gmail quota).
- Skip behaviour for users with stale/revoked `gmail_refresh_token`.

### 1.5 `check_for_inbox_drift` scope
"Pull all ids from oldest backend thread onward" is potentially thousands of ids. Needs a bound (current inbox window only? last N days? hard cap on ids fetched?).

### 1.6 Concurrency / locking
Current code uses `sync_lock:{uid}` (NX EX 600) to serialise per-user sync work. Spec is silent on this.

Decisions needed:
- Keep the per-user lock?
- Behaviour when reclassify, poll, and extend all want to run.
- `recent_message_id` selection rule when messages arrive out of order.

### 1.7 Cursor-advancement semantics on failure
If `partial_sync` writes some threads then crashes mid-batch, does `gmail_last_history_id` advance or not? Today it advances at the end of the batch; spec should pin the contract explicitly.

### 1.8 Gmail API concurrency cap
Anthropic has `ANTHROPIC_CONCURRENCY=16`; Gmail has no cap defined. Bulk paths (especially `load_from_zero=500`) are worth bounding.

### 1.9 `load_from_zero` size
Spec says 500 threads, current code does 200. Intentional bump? Affects first-load latency materially.

---

## 2. backend ⇄ frontend

### 2.1 Live channel: websocket or SSE
`flows.md` and `inbox-ops.md` say "websocket"; `ARCHITECTURE.md` shows SSE. If switching, that's a substantive change (lib choice, framing, auth-on-upgrade, reconnect strategy). If keeping SSE, fix the terminology.

### 2.2 Thread state machine
States `ingested → processing → up-to-date` are named but not pinned.

Decisions needed:
- Who emits each transition (worker stage boundary?).
- Event payload shape per transition.
- Client rendering per state.
- Terminal failure states (`failed`? `retrying`?).
- Per-thread status vs an additional global "sync in progress" indicator.

### 2.3 Pagination contract
Decisions needed:
- Page size (25 / 50 / 100).
- "Total threads" source: backend-known count or Gmail `resultSizeEstimate`.
- Stability: when new messages arrive while user is on page 2, does page 2 reshuffle, or is the page snapshot frozen until the user navigates?

### 2.4 New-mail-while-browsing UX
Insert at top of page 1 only? Show a "N new" banner? Spec is silent.

### 2.5 Two polling layers
Spec adds a client-side 30s `poll-for-inbox-updates` request, while the existing 30s beat `enqueue_polls` already polls active users. Pick one canonical layer or document why both exist.

### 2.6 Multi-tab
EventSource is per-tab today. With websocket (if we switch): per-tab? Shared via BroadcastChannel? Where does page state live?

### 2.7 Auth + reconnect
- Cookie carry-through on WS upgrade.
- Reconnect backoff.
- Server-side buffering during client disconnect (or accept that the client must resync on reconnect).

### 2.8 Burst / backpressure
Bucket create triggers `reclassify_user_inbox` which can update hundreds of threads at once.

Decisions needed:
- Queue bounds for per-tab event streams.
- Coalescing / batching of update events.
- Ordering guarantees the client can rely on.

### 2.9 Inbox mutations from client
Will the HUD let the user archive / mark read / move buckets from our UI?
- If yes: write-path sync (us → Gmail) belongs in this spec, with conflict resolution rules.
- If no: declare explicitly out of scope.

---

## 3. Cross-cutting

### 3.1 Migration plan
Existing prod has: 200-thread sync window, no blob storage, `body_preview` only, `historyTypes=messageAdded` only. New spec implies a different shape.

Decisions needed:
- Backfill bodies/attachments for existing threads, or forward-fill only on next poll?
- Migration order (schema → blob plumbing → cutover).
- Rollback path.

### 3.2 Observability
None of the spec docs mention metrics or logs. Worth defining:
- Sync lag (now − `gmail_last_history_id` age).
- Drift-detection counter (threads found by `check_for_inbox_drift`).
- Queue depth, attachment-fetch failures.
- History-cursor expiration events.

### 3.3 Acceptance criteria
Spec has no "done = X" definition for the sync feature. Need testable criteria (e.g. "an archive in Gmail is reflected in the UI within N seconds for active users").

### 3.4 Litellm migration
`flows.md` mentions switching the LLM client to litellm. This is unrelated to inbox sync — recommend moving to its own spec dir so it doesn't dilute this one.

### 3.5 Interaction with existing flows
The spec doesn't say how new sync rules interact with:
- `reclassify_user_inbox` (touches the same threads, currently retries on `sync_lock`).
- `bucket_draft_preview` (reads thread bodies via `gmail.threads.get` directly — should it use blob storage once that exists?).
Either confirm they're unchanged, or describe how they fold into the new state machine.
