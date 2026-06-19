<!-- stamp: 6fbb58d (main) | 2026-06-12 | Agent 2 — data-layer first -->

# Event Propagation — durable per-user event log + resumable SSE

> Goal: **G6** — every backend state change (new mail, enrichment, task state)
> reaches the frontend live, in order, with replay on reconnect. Extends the
> existing `server/app/realtime/` layer; no transport swap.

## 1. Decision: keep SSE (closes OQ 2.1), add a Redis Stream log

WebSocket buys bidirectionality we don't need (client→server is plain HTTP)
and costs a new auth-on-upgrade path, reconnect protocol, and library on both
ends. SSE already works through the cookie auth in `app/deps.get_current_user`
and `EventSource` gives **automatic reconnect with `Last-Event-ID`** — the
exact primitive a replayable log wants. The 002 spec's "websocket" wording is
corrected to SSE, permanently.

Today's defect isn't the transport; it's that frames are transient
(`PUBLISH` to `user:{uid}`, dropped at `put_nowait` on full queues, dropped
when `subscribers=0`). Fix: make the log durable, keep pub/sub as wakeup.

## 2. The event log

New module **`server/app/realtime/event_log.py`**:

```python
def append(user_id: str, event: str, payload: dict) -> str:
    """XADD events:{user_id} MAXLEN ~ 1024 — returns the stream id.
    Then PUBLISH user:{user_id} (wakeup ping carrying the stream id)."""

def read_since(user_id: str, last_id: str, count: int = 256) -> list[(id, dict)]:
    """XRANGE events:{user_id} (last_id, +] — replay for reconnects."""
```

- Stream key `events:{uid}`, `MAXLEN ~1024` (approximate trim — bounded
  memory, ~hours of history; older gaps fall back to snapshot).
- `tasks._publish` in `server/app/workers/tasks.py` is rewritten to call
  `event_log.append` (one writer seam, same as today — every publisher
  already goes through `_publish`).
- Redis stays one instance, four roles now (broker, pub/sub wakeup, KV,
  streams). Same `REDIS_URL`.

## 3. SSE changes (`server/app/api/sse.py`)

- Each frame is emitted as `id: <stream-id>\ndata: <json>\n\n`.
- On connect, read `Last-Event-ID` header (browsers send it automatically on
  reconnect). If present: `event_log.read_since` and flush the gap **before**
  going live; if the id has been trimmed out of the stream (XRANGE from a
  too-old id returns from the trim point — detect by comparing against the
  stream's first entry), emit a synthetic `data: {"event":"resync_required"}`
  so the client snapshots.
- The `PubSubDispatcher` (`server/app/realtime/pubsub.py`) keeps its shape but
  frames it routes are now `(stream_id, body)` tuples; `QueueFull` drops
  become harmless — the client heals via `Last-Event-ID` on its next
  reconnect, and a dropped frame triggers a forced per-connection
  `resync_required` push (the handler knows it dropped).
- `sse_connections.py` and `active_users.py` are unchanged.

## 4. Client changes

- `client/src/lib/sse.ts`: native `EventSource` already resends
  `Last-Event-ID`; add handling for `resync_required` (broadcast as a
  synthetic event, like `_open`/`_error` today).
- `client/src/pages/inbox/useInboxSse.tsx`: the buffer-until-snapshot dance
  survives, but the **watchdog timers die**: `Home.tsx`'s 60s/150s reclassify
  resync and the `EXTEND_TIMEOUT_MS=90_000` watchdog in `useInbox.tsx` exist
  only because delivery was lossy. With replay, `extend_complete` and
  reclassify completion are guaranteed-or-resync. (Keep the extend watchdog
  one release as a belt; delete after soak.)
- New consumer hooks (`useTaskEvents`, Phase 4) subscribe to the same
  singleton — no new connection per feature.

## 5. Event vocabulary (typed, versioned)

All events carry `{"event": <type>, "v": 1, ...}`. Existing three are
preserved; new ones added per phase:

| Event | Payload | Producer | Phase |
|-------|---------|----------|-------|
| `threads_updated` | `{thread_ids}` | sync/reclassify (unchanged) | exists |
| `extend_complete` | `{thread_ids, more}` | extend task (unchanged) | exists |
| `bucket_draft_preview` | `{draft_id, positives, near_misses}` | preview (unchanged) | exists |
| `thread_upserted` | `{thread_ids}` | ingest commit, **before** enrichment | 3 |
| `thread_enriched` | `{thread_ids}` | after classify/extract commit | 3 |
| `thread_removed` | `{thread_ids}` | archive/delete mirror | 2 |
| `sync_status` | `{state: syncing\|idle, scope}` | task start/finish | 3 |
| `task_created` / `task_updated` | `{task_id}` | task CRUD | 4 |
| `task_state_changed` | `{task_id, entity_ids, event_ids}` | extraction apply / correction | 4 |
| `task_review_pending` | `{task_id, count}` | extraction below threshold | 4 |
| `resync_required` | `{}` | SSE layer (gap too old / queue drop) | 3 |

## 6. Decoupled ingest/enrich (closes OQ 2.2)

Today a thread commits with classification already done
(`_classify_batch` inside `partial_sync_inbox`), so freshness is gated on the
LLM. Phase 3 splits the worker pipeline:

1. **Ingest:** parse + upsert + commit + `append(thread_upserted)`. New mail
   hits the HUD at Gmail-fetch speed (the ≤5s G1 budget), rendered with its
   prior bucket/task chips and `processing_state='enriching'`.
2. **Enrich:** a follow-up task (`enrich_threads(user_id, thread_ids)`) runs
   relevance classification (+ task extraction in Phase 4), commits, sets
   `processing_state='clean'` (or `'failed'` on terminal error — the OQ 2.2
   failure state, rendered as a retry affordance), `append(thread_enriched)`.

The thread state machine OQ 2.2 asked for is exactly:
`enriching → clean | failed`, carried on `inbox_threads.processing_state`,
transitions emitted as the two events above. Per-thread status chips + one
global `sync_status` indicator in the HUD header.

## 7. Pagination & burst answers (OQ 2.3, 2.4, 2.6, 2.7, 2.8)

- **2.3:** page size **50** (current `PAGE_SIZE`); total from backend count
  (`SELECT count(*)` is fine at this scale; never Gmail's
  `resultSizeEstimate`); pages are **live** (current LWW re-sort behavior in
  `useInbox.applyThreadUpdates` stays — the HUD demotes the inbox to a
  spot-check surface, so reshuffle is acceptable; a "N new" pill (2.4) shows
  on non-first pages instead of yanking scroll position).
- **2.6 multi-tab:** per-tab `EventSource` stays (pinned). Tabs are cheap
  (one stream read each), `BroadcastChannel` is YAGNI.
- **2.7:** reconnect/backoff = native EventSource + the existing microtask
  reopen in `lib/sse.ts`; server buffering = the stream (§2); gap-too-old =
  snapshot resync.
- **2.8 burst:** reclassify publishing hundreds of ids stays **one** event
  (ids are batched at the producer; `threads_updated` is already a batch).
  Stream MAXLEN bounds memory; ordering is the stream order, which is commit
  order per user (all writers hold `sync_lock:{uid}`, so per-user ordering is
  already serialized — pinned as the ordering guarantee the client may rely
  on).
