<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 2 — data-layer first -->

# Sync Architecture — Gmail push via Pub/Sub + reconciliation

> Goal: replace "30s beat poll for SSE-connected users" with a push-first
> architecture that hits **G1 (≤5s freshness)**, **G2 (completeness: archives,
> deletes, labels)**, and **G3 (no identity-destroying full sync)** — while
> keeping every existing fallback path (`HistoryGoneError` → full sync) intact.

## 1. Decision: `users.watch` push, not adaptive polling

**Committed: Gmail `users.watch` → Google Cloud Pub/Sub → push subscription →
`POST /api/gmail/webhook` → enqueue existing `poll_new_messages`.**

Why push wins over adaptive polling here:

- **Latency floor.** Polling's best case is the poll interval; hitting ≤5s by
  polling means a 5s tick × every active user × `history.list` per tick —
  quota burn that scales with users, not with mail volume. Push notifications
  arrive in ~1s and cost nothing when no mail arrives.
- **Inactive users come free.** A watch lasts 7 days regardless of SSE
  connection, so task state advances while the user is away — exactly what a
  task HUD needs (the current `active_users`-gated poll only syncs people who
  are looking at the page). This dissolves open question 1.4's "hourly
  inactive poll" design problem almost entirely.
- **Marginal infra is tiny.** We already have a GCP project (the OAuth
  client). Adding: one Pub/Sub topic, one push subscription, publish grant to
  `gmail-api-push@system.gserviceaccount.com`. No new Railway service, no new
  datastore.
- **The poll machinery is not deleted** — it is demoted to the reconciliation
  net (Pub/Sub is at-least-once but not guaranteed-delivery in practice;
  watches expire; webhooks blip). Push = fast path; poll = correctness floor.

### 1.1 Mechanics

New module **`server/app/gmail/watch.py`**:

- `start_watch(gmail, *, topic_name) -> (history_id, expiration_ms)` — wraps
  `users.watch(userId="me", body={"topicName": settings.gmail_pubsub_topic,
  "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"})`.
- `stop_watch(gmail)` — `users.stop` (called on logout/token revocation).

New columns on `users` (migration `0006`, see storage doc):

```python
gmail_watch_expiration: Mapped[int | None]   # ms epoch from users.watch response
gmail_sync_status: Mapped[str]               # 'ok' | 'revoked' | 'watch_failed', server_default 'ok'
```

New route **`POST /api/gmail/webhook`** in `server/app/api/gmail.py`
(unauthenticated by session — authenticated by Pub/Sub OIDC):

1. Verify the `Authorization: Bearer <jwt>` OIDC token from the push
   subscription (`google.oauth2.id_token.verify_oauth2_token`, audience =
   `settings.gmail_webhook_audience`). Reject 403 otherwise.
2. Decode `message.data` (base64 JSON `{"emailAddress", "historyId"}`).
3. Map `emailAddress` → `users.email`; unknown → ack 200 (don't make Pub/Sub
   retry forever for a deleted user).
4. **Debounce**: `SET push_debounce:{uid} 1 NX EX 3` in Redis. If set,
   `poll_new_messages.apply_async([uid])`; if already present, skip — the
   in-flight/imminent sync's `history.list` reads from the cursor and will
   absorb this notification's changes. A change arriving in the narrow window
   after an in-flight sync's `history.list` call is caught by the next
   notification or the 90s reconciliation poll (pinned, acceptable).
5. Always return 200 fast (Pub/Sub redelivers on non-2xx; we never want
   redelivery storms for app-level conditions).

`poll_new_messages` in `server/app/workers/tasks.py` is **unchanged in
shape** — push just calls it more promptly. This is the key conservatism:
push is a trigger source, not a new sync engine.

### 1.2 Watch lifecycle (beat)

New beat entries in `server/app/workers/beat_schedule.py` (beat stays one
replica):

| Entry | Schedule | Task |
|-------|----------|------|
| `renew-watches-hourly` | 3600s | `renew_gmail_watches` — select users where `gmail_watch_expiration` is NULL or < now+24h and `gmail_sync_status='ok'`; spread `start_watch` calls with `countdown=hash(uid) % 3000` (thundering-herd guard, answers OQ 1.4 sharding); on `invalid_grant` set `gmail_sync_status='revoked'` and skip forever until re-auth. |
| `enqueue-polls-every-90s` | 90.0 (was 30.0) | existing `enqueue_polls` — demoted reconciliation for active users. |
| `sweep-all-users-hourly` | 3600s | `sweep_inactive_users` — `poll_new_messages` for every `gmail_sync_status='ok'` user **not** in `active_users`, spread over the hour by `hash(uid) % 3600`. Cheap: cursor-based `history.list` returns empty for idle inboxes. |
| `drift-sweep-daily` | 86400s | `check_inbox_drift` — see §4. |

Also: `auth/google_oauth.py` callback gains a post-login
`start_watch` kickoff (enqueue, not inline); `auth` logout does **not** stop
the watch (the user wants tasks to keep tracking — that's the product).

New settings in `server/app/config.py`: `GMAIL_PUBSUB_TOPIC`,
`GMAIL_WEBHOOK_AUDIENCE`, `GMAIL_CONCURRENCY` (§5).

## 2. Completeness: mirror archive / delete / labels (OQ 1.1)

`fetch_history_records` (`server/app/workers/gmail_sync.py`) changes:

```python
historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
labelId="INBOX",
```

`partial_sync_inbox` gains handling beyond `messagesAdded`:

- `messagesDeleted` → mark `inbox_messages.is_deleted = true` (soft — task
  evidence must survive; G3). If a thread's messages are all deleted, set
  `inbox_threads.is_archived = true`.
- `labelsRemoved` containing `INBOX` → set `inbox_threads.is_archived = true`
  (archive mirror). `labelsAdded` containing `INBOX` → clear it (move back to
  inbox).
- `labelsAdded`/`labelsRemoved` for `UNREAD`/`STARRED` → update
  `inbox_messages.labels` (JSONB) and the denormalized
  `inbox_messages.is_unread`.

Decisions, explicitly (answering 1.1's checklist): mirror archive **yes**;
mirror delete **yes (soft)**; track read/unread + starred **yes** (the HUD
recent-activity feed wants them); arbitrary other labels stored in the JSONB
but not interpreted.

Default read paths (`inbox_repo.list_threads`, search) filter
`is_archived = false` unless the caller asks for archived (HUD task views ask
— a thread archived in Gmail is still task evidence).

## 3. Full sync becomes reconciliation (G3; OQ 1.7, 3.1)

`full_sync_inbox` currently wipes (`inbox_repo.clear_user_inbox`) then
repopulates. **This must die before task tables exist** — `task_links` /
`task_events` FK onto `inbox_threads.id` and a `HistoryGoneError` would orphan
a task's evidence.

Rewrite (same file, same signature):

1. `threads.list(maxResults=200, labelIds=["INBOX"])` → fetch + parse +
   classify + **upsert** (exactly the current loop, minus the wipe — upserts
   are already idempotent via `uq_inbox_threads_user_gmail`).
2. Collect the listed gmail ids; any stored thread with
   `is_archived = false` whose `gmail_id` is **not** in the listing and whose
   `last_activity_at` falls inside the listed window → set
   `is_archived = true` (it left the inbox while our cursor was dead). Never
   delete rows.
3. Advance the cursor as today (max `gmail_history_id` seen).

`clear_user_inbox` in `server/app/inbox/inbox_repo.py` survives only for
account deletion.

**Cursor contract (OQ 1.7, pinned):** `gmail_last_history_id` advances only
after the whole batch commits (current behavior, now the explicit contract).
A crash mid-batch leaves the cursor unmoved; the re-run re-ingests
idempotently through the upserts. Per-thread `threads.get` 404s are tolerated
(current try/except stays) so one dead thread can't pin the cursor.

## 4. Drift sweep (OQ 1.5, bounded)

New task `check_inbox_drift(user_id)` (daily beat, inactive users first):

- Bound: `messages.list(q="after:<oldest stored thread's date>",
  labelIds=["INBOX"])`, **max 10 pages / 5,000 ids** (pinned cap). Compare id
  sets both directions:
  - in Gmail, missing here → enqueue ingest for those threads;
  - here (`is_archived=false`), missing in Gmail's listing → mark archived.
- Skips users with `gmail_sync_status != 'ok'`.
- Emits a counter log line (`drift_found=N`) — the observability hook OQ 3.2
  asks for (structured logs are the v1 metric system; pinned, YAGNI on a
  metrics stack).

## 5. Gmail fetch concurrency (OQ 1.8) and window size (OQ 1.9)

- The sequential `threads.get` loops in `full_sync_inbox`, `partial_sync_inbox`
  and `extend_inbox_history` move to a bounded
  `concurrent.futures.ThreadPoolExecutor(max_workers=settings.gmail_concurrency)`
  (default **5**; googleapiclient is sync, threads are the idiomatic bound).
  200-thread full sync: ~40s → ~8s. Note each thread needs its own client via
  `get_gmail_client` or per-call `http` — `googleapiclient` is not
  thread-safe on a shared http object.
- `load_from_zero` window stays **200** (pinned; the 002 spec's 500 buys
  first-paint latency for little — deeper history arrives via extend and
  task-creation backfill).

## 6. Remaining 002 open questions, resolved

| OQ | Resolution |
|----|------------|
| 1.2 body/attachments | **No blob store.** Full text in Postgres (`agent-2-02`); attachments = metadata table + lazy auth-gated proxy `GET /api/messages/{id}/attachments/{aid}` streaming from Gmail (`users.messages.attachments.get`) at view time. Gmail *is* the blob store; we keep refs. Caps/retention/encryption questions dissolve. |
| 1.3 fetch timing | Lazy (above). |
| 1.4 inactive poll | Superseded by watch + hourly sweep (§1.2); sharding via `hash(uid)` spread; revoked tokens short-circuit on `gmail_sync_status`. |
| 1.6 locking | **Keep `sync_lock:{uid}`** exactly as is (`server/app/realtime/sync_lock.py`); push webhook enqueues respect it (skip-if-held is fine — cursor semantics make skipped runs harmless). `recent_message_id` rule stays the indexed `ORDER BY gmail_internal_date DESC LIMIT 1` in `inbox_repo.upsert_message`; out-of-order arrivals are self-healing because every upsert recomputes it. |
| 2.5 two polling layers | Client-side polling is **never added**. Canonical layer: push → worker → Redis → SSE. Beat = reconciliation only. |
| 3.3 acceptance | Pinned: (a) new Gmail message visible in a connected HUD ≤5s p50 / ≤90s p99; (b) archive in Gmail reflected ≤5s for active users, ≤1h inactive; (c) `HistoryGoneError` recovery preserves all `inbox_threads.id`s; (d) drift sweep over a 30-day-idle account finds <1% divergence. |
| 3.4 litellm | Out of scope here (already superseded — the LLM client is OpenRouter via `AsyncOpenAI`, `server/app/config.py`). |
| 3.5 interactions | `reclassify_user_inbox` unchanged (still lock+retry); `draft_preview_bucket`'s `_score_all` stops refetching Gmail and reads `body_text` from Postgres (Phase 0) — its sequential-fetch bottleneck disappears. |

## 7. Write path: client mutations (OQ 2.9 — yes, scoped)

The HUD will archive / mark-read from our UI (Phase 6, with the action scope
upgrade): `POST /api/threads/{id}/modify {archive|unarchive|read|unread}` →
worker task calling `users.messages.modify` → local row updated on the
history event that comes back (Gmail is the authority; we write through and
let the watch echo confirm). Until Phase 6 this is explicitly out of scope.
