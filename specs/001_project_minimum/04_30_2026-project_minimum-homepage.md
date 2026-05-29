
## overview
inbox of email threads in postgres, mirrored from gmail. while user is online,
system polls gmail every 30s for changes and pushes updates to the browser
over sse. browser keeps a local cache in sync.

components: client, api server, celery workers, celery beat, redis, postgres, gmail.

## data flow
beat ticks every 30s -> fan-out task reads active-user registry in redis ->
enqueues one poll_new_messages per active user -> workers call gmail's
users.history.list, write changes to postgres, publish updated thread ids to a
redis pub/sub channel keyed on userId -> api's pub/sub dispatcher receives ->
forwards as an sse event to user's connection -> browser fetches the affected
threads and updates local state.


## flows

### initial page open
user is auth'd via session cookie. <Home/> mounts.
1. client opens sse to /sse/{userId}.
   - api adds userId to `active_users` in redis (so the periodic 30s poll
     covers them going forward).
   - api enqueues an immediate kickoff sync, countdown=0:
     full_sync_inbox(userId) if user.gmail_lastHistoryId is null, else
     poll_new_messages(userId). out-of-band of the periodic beat fan-out so
     the first fetch sees a fresh inbox without waiting up to 30s.
2. client buffers sse events without applying.
3. client calls GET /inbox?limit=200.
   - api reads postgres, returns 200 most recently active threads + `as_of`
     cursor.
   - first-time users: response may be empty until kickoff sync finishes.
     client shows a "syncing your inbox..." state and waits for sse events
     to populate. could also retry GET /inbox after the first sse event.
4. client replaces local state with the response.
5. client replays buffered sse events, dropping any covered by `as_of`.
   live events apply from this point.

(this same connection lifecycle re-runs on every sse reconnect.)

### detecting and handling new messages
beat fans out every 30s to every user in `active_users`. workers poll gmail
via users.history.list, write changes to postgres, publish updated thread
ids on `user:{userId}`. api dispatcher delivers as sse to all of that user's
open tabs. client per event:
 - GET /threads/{threadId} for each id, drop if stale per gmail_internalDate.
 - update display layer.
 - re-sort id layer by recent gmail_internalDate.
 - if user is on page 1, ui re-renders. on a later page, only id layer
   changes; display fetch deferred until they navigate.

### flipping between pages
- ids in id layer always reflect latest order (sse keeps it sorted).
- on navigate, check if display layer has data for destination page's ids.
  if not, GET /inbox?page=N hydrates them.

### keeping the client's copy in sync
covered by the two flows above. id layer is the live, always-sorted source
of truth, updated immediately on sse. display layer is hydrated lazily for
the current page. as_of cursor + sse-buffer-then-replay closes the gap on
(re)connect.

### user signs out / closes tab
sse connection drops. api removes that tab's queue from the connections map.
if no remaining connections on this server for the user, UNSUBSCRIBE from
`user:{userId}` and remove from `active_users`. next beat tick stops polling
them. on transient disconnect, eventsource auto-reconnects and the lifecycle
above re-runs.


## redis
three roles, one instance.

### celery broker/backend
job queue + beat schedule.

### active-user registry
sorted set `active_users`, score = expiry timestamp.
api writes on sse connect, refreshes via heartbeat every 20s while connected
(ttl 60s), removes on disconnect.
self-heals if api crashes — expired entries get purged by the fan-out task
before each tick.

### pub/sub bus
per-user channels `user:{id}`.
api subscribes on first sse connection for that user, unsubscribes when last
connection drops.
workers publish updated thread ids after writing to postgres.


## celery beat
singleton scheduler. own process, replicas = 1 (multiple beats = duplicate fan-outs).
fires one task every 30s: enqueue_polls. beat itself does no polling logic, just ticks.


## celery workers
stateless. two tasks.

### enqueue_polls (fan-out, fired by beat)
 - purge expired entries from `active_users`
 - read remaining user ids
 - for each, enqueue poll_new_messages(userId) with random 0-10s countdown
   to avoid thundering-herd against gmail and our db

### poll_new_messages(userId)
 - call users.history.list with that user's gmail_lastHistoryId from postgres.
   - no 404 handling — this only runs while user is online, and the client's
     resync-on-connect handles full sync recovery before subscribing.
 - if no new history records, return silently.
 - otherwise run partial_sync_inbox (below), which writes to postgres and
   returns affected thread ids.
 - publish those ids to redis channel `user:{userId}`.

### partial_sync_inbox(userId, history_records=None)
called by poll_new_messages. also reusable for full sync.
 - if history_records is null, fetch via users.history.list from the user's
   stored gmail_lastHistoryId.
 - process messagesDeleted: delete matching rows from inbox_messages; if a
   deleted message was the thread's recentMessageId, recompute it from
   remaining messages (or delete the thread if empty). out of scope for v1
   since users can't delete.
 - process messagesAdded: collect thread ids, pull full threads from gmail
   (users.threads.get), parse via message parser + thread assembler into a
   string representation including headers, bodies, and attachments.
 - run each thread through classification pipeline (dummy for now, eventually
   assigns bucketId). see workers spec for the classification pipeline.
 - write to postgres in a transaction:
   - update users.gmail_lastHistoryId to latest seen
   - upsert inbox_messages (threadId, gmail_id, gmail_threadId,
     gmail_internalDate, gmail_historyId, to, from, body_preview)
   - upsert inbox_threads (subject, bucketId, recentMessageId — recomputed as
     the message in this thread with max gmail_internalDate)
 - return new/updated thread ids.

### message parser
gmail message data model:
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages#Message
important fields: id, threadId, internalDate (int64), historyId, payload (MessagePart).

MessagePart: headers, body, parts.
generally only need the top-level MessagePart. parts is for multipart MIME,
don't need to worry about that here.

Header: name (eg "To", "From", "Subject"), value.

MessagePartBody: data (base64-encoded — normal email text lives here),
attachmentId. attachments require messages.attachments.get on top.


## api server
owns sse connections in a per-process in-memory map:
userId -> set[asyncio.Queue] (one queue per browser tab, since one user can
have multiple tabs open).
runs one background pub/sub dispatcher task that listens to all subscribed
redis channels and routes messages into the right queues.

### sse endpoint /sse/{userId}
 - create bounded queue (size 100), register under user's set in connections map.
 - if first connection on this server for this user, SUBSCRIBE to
   `user:{userId}` and add user to `active_users` in redis. enqueue an
   immediate kickoff sync (countdown=0): full_sync_inbox(userId) if
   user.gmail_lastHistoryId is null, else poll_new_messages(userId). this
   runs out-of-band of the periodic beat fan-out so the user's first inbox
   fetch sees fresh data instead of waiting up to 30s.
 - start heartbeat task that refreshes the active_users ttl every 20s.
 - stream loop: pull from queue with 20s timeout. on message:
   `data: {json}\n\n`. on timeout: `: keepalive\n\n` (prevents proxy idle-close).
 - on disconnect: remove queue from map, cancel heartbeat. if user has no
   remaining connections on this server, UNSUBSCRIBE and remove from
   active_users.

### pub/sub dispatcher
one task, started at app boot. listens on the shared pub/sub connection. on
each message, parses userId from channel name, looks up local queues for that
user, calls put_nowait on each. if a queue is full (slow consumer), drop the
message.

### inbox api endpoints
called by the client over normal http.
 - GET /inbox?limit=200 — most recently active threads with thread headers and
   message body previews. includes an `as_of` cursor (server timestamp or max
   gmail_historyId seen) used by the client to dedupe buffered sse events.
 - GET /inbox?page=N — same shape, for pagination beyond the initial 200.
 - GET /threads/{threadId} — single thread fetch, used after sse events.

set `X-Accel-Buffering: no` on the sse response so reverse proxies don't buffer.


## client
two layers of state.

### id layer
ordered list of thread ids representing the user's inbox in display order
(most-recent-message-first by gmail_internalDate).
updated immediately on sse events. source of truth for ordering and pagination.

### display layer
keyed by thread id. holds thread headers and message body previews (first 100 chars).
hydrated lazily — populated for the page the user is currently viewing. the
list view doesn't require full message bodies; users don't click into threads.

### connection lifecycle
also runs on every reconnect.
 1. open sse connection but buffer events without applying.
 2. fetch most recent 200 threads via GET /inbox?limit=200. response includes
    an `as_of` cursor.
 3. if user is viewing a page beyond thread 200, additionally fetch that page.
 4. replace local state with fetch result.
 5. replay buffered sse events, discard any whose updates are already covered
    by `as_of`. begin applying live events.

subscribe-then-snapshot closes the gap where events arriving between snapshot
and subscribe would be lost.

### sse event handling
events contain a list of new/updated thread ids. for each:
 - GET /threads/{threadId} to pull the latest version. drop the response if
   its gmail_internalDate is older than what's already in local state
   (prevents out-of-order GET responses from rolling state backwards).
 - update display layer with the fetched thread.
 - re-sort id layer by each thread's most recent gmail_internalDate.
   full re-sort, not prepend — a "new" thread might still belong below an
   older thread that has a newer recent message.
 - if user is on page 1, ui re-renders. if on a later page, only the id layer
   changes; the display update happens when they navigate.

### pagination
when navigating to a new page, check whether the display layer has data for
those thread ids. if not, fetch them.

### deletions
out of scope for v1.

### Home page UI
top bar across the page:
 - left: app name
 - right: user's name + small menu button. menu has "sign out".
table list of threads, 50 items per page.
each item is [{A} {subject} {lastMessageBodyPreview}]
 - A = whoever the other user in convo is if only one other in thread. if
   multiple, comma-separated list each one abbreviated.


## why this shape
 - polling lives in workers, not the api, so sse connection handling and the
   polling loop scale independently and don't compete for resources.
 - beat is separate because schedule ticks must not multiply if the api or
   workers scale.
 - redis pub/sub between worker and api decouples the writer (worker) from
   the reader (api connection holder); the worker doesn't need to know which
   server holds the user's connection.
 - active-user registry with ttl lets beat enumerate who needs polling
   without coupling beat to connection state. heartbeats make it
   crash-resilient.
 - two-layer client state lets sse events be cheap (just ids, immediately
   reflected in ordering) while keeping display layer hydrated only for what
   the user actually sees.
 - subscribe-before-snapshot with `as_of` dedupe is the standard pattern for
   live-syncing a paginated remote dataset without losing events at the seam.


## railway
need start commands per service now. railway.toml for current web service,
plus two new ones overriding the default start command.
railway.worker.toml
railway.beat.toml
