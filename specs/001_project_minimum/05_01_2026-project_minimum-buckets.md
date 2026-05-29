

## overview
inbox threads classified into buckets so users can triage. each thread
belongs to exactly one bucket or none. buckets are either default (seeded,
shared across users) or custom (created by a user, scoped to that user). a
classifier picks one bucket per thread using llm + per-bucket criteria.

a thread can only belong to one bucket.


## ui

new secondary header below the existing top bar. left → right:
 - reload button (moved out of the top bar)
 - filter-by-bucket dropdown
 - view buckets
 - new bucket


### filtering by bucket
checkbox-format multi-select dropdown listing all active buckets (defaults +
this user's non-deleted custom buckets) plus an "unclassified" pseudo-option
for threads with bucket_id null OR a bucket_id that doesn't resolve to an
active bucket. defaults to all selected.

filter operates on the **display layer**, not the id layer. idLayer keeps
every id (so unfiltering doesn't trigger a refetch). when a filter is
active, render walks idLayer in order and keeps only entries whose
displayLayer entry matches the active set; the kept entries are concatenated
and paginated normally — 50 per page over the *filtered* sequence. the
displayed page count reflects the filtered count.

threads in idLayer that aren't yet in displayLayer (e.g., older pages not
hydrated) don't appear under any active filter — there's no bucket_id to
match against. they pop in once hydrated.

unfiltering reverts to paginating over idLayer at full size.


### view buckets
modal listing every bucket (defaults + this user's non-deleted custom
buckets) with name + criteria. defaults render read-only. custom buckets
render with two actions:
 - **edit name** — inline rename.
 - **delete** — soft delete (see /api/buckets/{id} below). modal confirmation
   tells the user: *"deleting this bucket means no new threads will be
   classified into it. threads currently classified into this bucket keep
   that classification but show as unclassified going forward, and the
   bucket won't appear in your filter dropdown."*

editing the criteria of an existing custom bucket is **out of scope for v1**.
to refine a bucket, delete and re-create.


### creating a custom bucket
modal flow.

step 1 — name + free-text description ("what kind of email goes in this
bucket?").

step 2 — client posts to /api/buckets/draft/preview. server enqueues a
celery task; client gets 202 + a `draft_id`. results arrive over sse as a
`bucket_draft_preview` event keyed on draft_id (see "draft preview
pipeline" below). while waiting, modal shows a spinner.

each example in the result includes:
 - thread subject, sender
 - one-line llm rationale
 - **direct quotations and targeted snippets** from the thread that
   triggered the match. user has to be able to see *why* the llm picked it,
   not just trust the rationale.

step 3 — user clicks ✓ / ✗ on each example. ui suggests confirming ≥2
positives + ≥2 near-misses for a strong bucket but doesn't enforce it.
"more examples" button posts /preview again with `exclude_thread_ids` of
every example seen so far. loop until either:
 - user has enough confirmed examples and clicks save, or
 - user clicks "done" with whatever they have (including zero — modal
   surfaces a hint that 2+2 is the recommended floor, but accepts any
   count).

when /preview detects it's running thin on candidates (see pipeline below),
it auto-extends inbox history before sampling further. client doesn't have
to coordinate this.

step 4 — client posts /api/buckets with name, description, and the
confirmed examples. server formulates the final `criteria` text:

> *summary paragraph of what belongs in this bucket and what doesn't, then
> for each confirmed example the direct quotations + targeted snippets +
> the one-line llm rationale that the user agreed with — exactly the form
> the user saw and approved.*

format mirrors the structured default-bucket criteria (see "default
buckets") so the classifier sees the same shape regardless of source.

new bucket does NOT trigger reclassification of existing threads. only
threads upserted after creation (new messages, extend_inbox_history) get
classified against it.


## data model
defined in specs/04_28_2026-project_minimum-psql.md (#buckets,
#inbox_threads). three changes from that spec:

 - default bucket ids become uuid hex (was `default-important` etc.).
   migrated by name in migration 0003.
 - default `criteria` rewritten as a structured paragraph + tagged example
   blocks. see "default buckets" below.
 - new column on buckets: `is_deleted: bool not null default false`. soft-
   delete flag for custom buckets. defaults are never deleted.


## api endpoints
postgres is the source of truth. every bucket endpoint is a thin wrapper
over a buckets table query.

### GET /api/buckets
returns this user's *active* buckets only — defaults + custom rows where
is_deleted = false. soft-deleted buckets are omitted entirely.

shape: `[{id, name, criteria, is_default}]`.

a thread whose bucket_id refers to a soft-deleted (and therefore omitted)
bucket renders client-side as "unclassified" — the client treats any
unresolvable bucket_id the same as null.

called once at home-page mount.

### POST /api/buckets
body: `{name, description, confirmed_positives, confirmed_negatives}`.
server formulates the final `criteria` text from those inputs (per "step 4"
above), inserts the row with user_id = current user and is_deleted = false,
returns the new row. no reclassification of existing threads.

### PATCH /api/buckets/{id}
edit name only on a custom bucket the user owns. body: `{name}`. defaults
or buckets owned by other users → 403. **criteria editing is not in v1.**

### DELETE /api/buckets/{id}
soft delete on a custom bucket the user owns. defaults → 403. sets
`is_deleted = true`. inbox_threads.bucket_id is **not modified** — existing
classifications stick in the column but render as unclassified going
forward (since GET /api/buckets won't return the row).

side effects:
 - bucket disappears from filter dropdown + view-buckets modal.
 - classifier no longer considers it (excluded from `_available_bucket_ids`).
 - existing threads classified into it keep that bucket_id in postgres but
   the ui falls through to "unclassified" rendering.

### POST /api/buckets/draft/preview
body: `{name, description, exclude_thread_ids?}`.
returns `202 {draft_id}`. enqueues `draft_preview_bucket(user_id, draft_id,
name, description, exclude_thread_ids)`. results arrive over sse as a
`bucket_draft_preview` event whose payload includes the draft_id so the
client can route it back to the right modal session.


## default buckets

ids: uuid hex (migrated from `default-important` etc. in migration 0003).

criteria text is a description paragraph + tagged example blocks, exactly
the shape the classifier consumes:

**Important**
```
Threads where I'm a direct recipient and the other participants are
individuals or a company contacting me (not marketing) and I'm required to
act or respond.

Example cases:
<positive>
From: colleague@company.com
To: me
Subject: sprint meeting time

What time should we have the sprint meeting tomorrow?
</positive>
<positive>
From: counsel@lawfirm.com
To: me
Subject: Please review and sign — engagement letter

Attached is the engagement letter for our work together. Please review and
return signed by Friday.
</positive>
<nearmiss>
From: calendar-noreply@google.com
To: team-list@company.com
Subject: Weekly sync — 9 AM

You've been added as an optional attendee.
</nearmiss>
<nearmiss>
From: marketing@vendor.com
To: me
Subject: John, ready to upgrade?

Hi John, based on your usage we think you'd benefit from upgrading to Pro.
</nearmiss>
```

**Can wait**
```
Threads where I'm a recipient and may eventually want to read or respond,
but it's not urgent and can be batched. Internal announcements, FYIs,
non-blocking discussions.

Example cases:
<positive>
From: people-ops@company.com
To: all-staff@company.com
Subject: Reminder — open enrollment closes November 15

Open enrollment for benefits closes in two weeks. Submit your elections in
Workday.
</positive>
<positive>
From: teammate@company.com
To: project-channel@company.com
Subject: Re: design doc for the new dashboard

Sharing a draft for feedback whenever folks have a chance — no rush.
</positive>
<nearmiss>
From: colleague@company.com
To: me
Subject: Can you review this PR before EOD?

Could use eyes on this before I merge.
</nearmiss>
<nearmiss>
From: notifications@github.com
To: me
Subject: [repo] CI failure on main

Build failed on commit abc1234.
</nearmiss>
```

**Auto-archive**
```
Automated, transactional, or system-generated notifications I don't need to
read or act on individually. Receipts, shipping updates, build
notifications, status pings.

Example cases:
<positive>
From: shipment-tracking@amazon.com
To: me
Subject: Your package has shipped

Your order #123-456 has shipped and will arrive Tuesday.
</positive>
<positive>
From: builds@ci.company.com
To: me
Subject: ✅ Build #4521 succeeded

main: build passed in 4m32s.
</positive>
<nearmiss>
From: security@bank.com
To: me
Subject: Unusual sign-in attempt detected

We noticed a sign-in from a new device. If this wasn't you, please review.
</nearmiss>
<nearmiss>
From: status@datadog.com
To: me
Subject: P1 incident — production API down

Datadog detected a P1 incident affecting the prod API.
</nearmiss>
```

**Newsletter**
```
Opted-in marketing, content subscriptions, and bulk-sends from publications
or vendors. Distinct from transactional automated mail (which is
Auto-archive) — these are read-as-content rather than processed-as-events.

Example cases:
<positive>
From: newsletter@stratechery.com
To: me
Subject: The end of the long tail

This week: how aggregation theory plays out in the AI era…
</positive>
<positive>
From: digest@substack.com
To: me
Subject: Your weekly digest from 5 publications

Here's what's new from the writers you follow.
</positive>
<nearmiss>
From: receipts@vendor.com
To: me
Subject: Your invoice for October

Invoice #INV-2026-10-1234 is attached. Total due: $42.
</nearmiss>
<nearmiss>
From: founder@startup.com
To: me
Subject: Quick favor — feedback on our beta?

Hey John, would love your take on what we shipped this week. 5-min
question if you have a sec.
</nearmiss>
```


## classification pipeline

### inputs
 - the thread (string representation via thread_to_string)
 - all *active* bucket criteria (defaults + this user's custom buckets where
   is_deleted = false), by id + name + criteria text
 - existing bucket_id on the thread, if any (for stability)

### llm
claude-haiku-4-5. **one llm call per thread.** prompt explicitly states
that "no bucket fits — return null" is a valid answer. on a no-fit response:
 - new thread → bucket_id stays null (thread is unclassified, falls into
   the "unclassified" filter pseudo-bucket).
 - existing thread being re-classified → keeps its current bucket_id rather
   than nulling out (avoids losing a previously good classification because
   one new message looked off-topic).

### batching / parallelism
classify(threads, buckets, current_bucket_ids) accepts a list of threads.
internally it issues one llm call per thread, in parallel via asyncio with
a semaphore so we don't blow past anthropic rate limits during a 200-thread
full sync. each call resolves to `{thread_index, bucket_id|null, reason}`.
classify() awaits all and unpacks into a list of bucket_ids matching input
order.

the semaphore is a **process-wide singleton in the llm client module**,
shared with the draft-preview pipeline. starts at 16 concurrent in-flight
calls. one budget for all anthropic traffic so a full-sync fan-out and a
user-initiated preview can't both push 16 concurrently and starve the rate
limit.

callers with a single thread (the partial-sync path on a new message) call
classify with a 1-element list. same code path; semaphore is a no-op at
n=1.

### classifying an unseen thread
 - pass criteria for active defaults + active custom buckets
 - ask: which bucket, if any, best fits

### classifying an existing thread with new messages
 - pass criteria for active defaults + active custom buckets
 - pass the current bucket_id
 - prompt tells the llm: "this thread is currently in {X}. only change the
   bucket if the new content makes a different bucket clearly more
   appropriate." prevents flip-flopping.

### when do we re-run classification on existing threads?
 - on a new message in the thread → yes, with stability hint above.
 - on bucket creation/edit/delete → no. v1 doesn't re-classify the
   back-catalog.


## draft preview pipeline

new celery task: `draft_preview_bucket(user_id, draft_id, name, description,
exclude_thread_ids)`.

reuses the same llm client + the same process-wide asyncio semaphore as
classification. preview calls a different prompt (score 0-10 against the
description) but shares the rate-limit budget.

flow:
 1. read candidate thread ids from postgres for this user, excluding
    `exclude_thread_ids`. take the most recent ~200 (more than the spec's
    earlier 30 — many positives appear rarely so we need a wider net).
 2. if the candidate pool is below a threshold (start at 100), call
    extend_inbox_history inline (same code path as the worker task) to
    pull older threads. acquire sync_lock for that step. release before
    scoring. re-read the candidate list after extend.
 3. for each candidate: build thread string via thread_to_string → ask the
    llm to score it 0-10 against the description, returning rationale +
    targeted snippets / quotations. parallelized via the shared semaphore
    (cap = 16, see classification pipeline).
 4. rank candidates by score. top 3 with score ≥ 7 → positives. top 3
    with score 4-6 → near-misses. discard the rest. (thresholds are a
    starting point; revisit after we see real data.)
 5. publish `bucket_draft_preview` event on `user:{user_id}` with payload
    `{draft_id, positives: [...], near_misses: [...]}`. each example
    contains thread id, subject, sender, score, rationale, quoted snippet.

scalability notes:
 - 200 candidates × 1 llm call each ≈ 13 batches at semaphore=16 — runs in
   tens of seconds on haiku-4-5 with reasonable input sizes.
 - candidate pool capped at 200 per call to bound cost. successive
   /preview calls use exclude_thread_ids to walk further into the inbox.
 - if the user keeps clicking "more examples" past their inbox depth, the
   inline extend keeps pulling older threads on each call until gmail runs
   out (signaled by extend returning <200 threads, see below).


## extending inbox history (older threads)
v1 only loads the most recent 200 threads. when the user paginates past the
end (or the new-bucket flow runs out of candidates), pull more older threads
on demand.

### worker task: extend_inbox_history(user_id, before_internal_date_ms)
 - acquire per-user sync_lock (shared with poll_new_messages and
   full_sync_inbox).
 - convert ms → seconds: `before_secs = before_internal_date_ms // 1000`.
 - call `users.threads.list(userId='me', q=f'before:{before_secs}',
   maxResults=200)`. gmail's `q` accepts the same operators as the search
   box; `before:<unix-seconds>` returns threads received strictly before
   that timestamp. ref:
   https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/list
   and search-operator docs at
   https://support.google.com/mail/answer/7190.
 - for each returned stub: `threads.get(format=full)` → assemble_thread →
   classify (1-element call) → upsert via _upsert_thread_with_messages.
 - **does not** clear inbox_threads / inbox_messages.
 - **does not** update users.gmail_last_history_id (cursor stays anchored
   at most-recent so partial sync continues working).
 - publish `extend_complete` event on `user:{user_id}` with payload
   `{thread_ids: [...], more: bool}`.
   - `thread_ids` is the list of internal InboxThread.id values upserted.
   - `more = (len(returned_stubs) == 200)`. if gmail returned fewer than
     200, we've hit the bottom of the user's inbox history (or close to it
     — 200 is the page cap, anything less means no further page).
 - the sse event also drives the existing display-layer hydration path:
   the client batch-fetches by id and merges into idLayer/displayLayer.

### POST /api/inbox/extend
body: `{before_internal_date}`. returns 202. enqueues
extend_inbox_history. result delivered via sse as above.

### client trigger
on navigate to a page where
`idLayer.length - page * PAGE_SIZE < PAGE_SIZE`
(last page is partial), or on the last page, post /api/inbox/extend with
the smallest gmail_internal_date in the current idLayer.

client tracks `more` from the most recent extend_complete event:
 - `more = true` → keep offering further extension.
 - `more = false` → mark "no more older threads" for the rest of the
   session; suppress further /api/inbox/extend calls. resets on reload.

### interaction with full_sync_inbox
full_sync_inbox calls clear_user_inbox before repopulating, so a recovery
full sync wipes any extended-history threads. acceptable for v1: full sync
fires only on history 404 (~30-day offline gap) or first login. user
re-extends after.

### why this is mostly additive
reuses _upsert_thread_with_messages, sync_lock, classify(). new task +
endpoint + client trigger. no schema changes (other than the unrelated
buckets.is_deleted column). no changes to partial_sync_inbox or
full_sync_inbox.


## migration plan
new migration 0003_buckets_v2 (idempotent up + down):
 1. add `is_deleted` column to buckets, default false, not null.
 2. insert four new uuid-hex rows for the default buckets with rewritten
    structured criteria.
 3. UPDATE inbox_threads SET bucket_id = (new uuid) WHERE bucket_id =
    'default-important' (and same for the other three).
 4. DELETE FROM buckets WHERE id IN ('default-important',
    'default-can-wait', 'default-auto-archive', 'default-newsletter') —
    old rows.
 5. downgrade reverses (insert old rows back, repoint inbox_threads, delete
    new uuid rows, drop is_deleted).


## what v1 does not do
 - re-run classification on existing threads when a bucket is created or
   deleted. only new messages (and extend-history fetches) trigger
   classification. follow-up if users want it.
 - edit a custom bucket's criteria. delete + recreate is the workflow.
 - reconcile extended-history rows after a recovery full_sync — they get
   wiped and the user re-extends.
 - server-side bucket filtering. all filtering is client-side on the
   display layer.
 - llm-driven topK pre-filter when bucket count grows large. classify
   sends every active bucket every call; fine for the bucket counts we
   expect (<20 per user).


## notes
bucket ids are uuids — including the default ones, after migration 0003.
when running classification we pull in criteria from the buckets table for
the active default buckets and for the user's active custom buckets
(is_deleted = false on both).
