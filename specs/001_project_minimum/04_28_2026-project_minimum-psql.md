
### Postgres

> the users table (incl gmail oauth token columns) and the sessions table are
> defined in the auth spec. tables below are the inbox/bucket data model.
> classification + default-bucket semantics live in the workers spec.

#### Conventions
- ids we copy from the gmail api get a `gmail_` prefix.
- our own primary keys are uuid hex strings (server-assigned).
- everything is CRUD-able: rows can be inserted, updated (eg `recent_message_id`,
  `gmail_last_history_id`), and deleted.

note: `users.gmail_last_history_id` (defined alongside the rest of users in
the auth spec) is the sync cursor — read/written by the partial/full sync
workers.


#### buckets
- `id`: string (uuid hex; default rows use stable string ids like
  `default-important` so seeds are idempotent across migrations)
- `user_id`: nullable fk to `users(id)`. null = a default bucket shared by
  all users
- `name`: string
- `criteria`: text (classification prompt fragment)

indexes:
- `ix_buckets_user_id` — covers both "this user's custom buckets" and
  "default buckets" (user_id is null).

seeded rows (created in migration `0002_inbox`):
- `Important`, `Can wait`, `Auto-archive`, `Newsletter` — see workers spec
  for what these mean to the classifier.


#### inbox_threads
- `id`: string (uuid hex, server-assigned)
- `user_id`: fk to `users(id)`
- `gmail_id`: gmail's threadId
- `subject`: text, nullable
- `bucket_id`: nullable fk to `buckets(id)`
- `recent_message_id`: string, nullable. logically points to the
  `inbox_messages.id` of the message in this thread with the largest
  `gmail_internal_date`. **intentionally not a real FK** — at insert time we
  haven't written the messages yet (chicken/egg), so it stays a soft pointer
  and is recomputed when messages change.

constraints + indexes:
- `uq_inbox_threads_user_gmail` unique on (`user_id`, `gmail_id`) — prevents
  duplicate rows when concurrent celery tasks (beat tick + sse-kickoff,
  retries after transient errors) race to insert the same thread.
- `ix_inbox_threads_user_id` — list-by-user queries.
- `ix_inbox_threads_gmail_id` — lookups during sync (history → thread → row).


#### inbox_messages
- `id`: string (uuid hex, server-assigned)
- `thread_id`: fk to `inbox_threads(id)`
- `user_id`: fk to `users(id)`
- `gmail_id`: gmail's messageId
- `gmail_thread_id`: gmail's threadId (denormalized so sync code can match
  records without a join)
- `gmail_internal_date`: int64 ms-since-epoch, mirrors gmail's
  `MessagePart.internalDate`
- `gmail_history_id`: string
- `to_addr`, `from_addr`: text, nullable
- `body_preview`: first 400 chars of decoded body text. the full body is
  decoded in-memory for the classifier and then discarded — never persisted.

constraints + indexes:
- `uq_inbox_messages_user_gmail` unique on (`user_id`, `gmail_id`) — same
  race-prevention rationale as on `inbox_threads`.
- `ix_inbox_messages_thread_id` — fast access to all messages in a thread.
- `ix_inbox_messages_user_id` — per-user list queries.
- `ix_inbox_messages_gmail_internal_date` — used to recompute a thread's
  `recent_message_id` and to order the inbox feed.


### Blob storage
not used in v1. headers + body preview live in postgres for the list view.
when classification runs, the worker pulls the full thread fresh from the
gmail api in-memory, classifies, and discards the full bodies. attachments
are not yet wired in — see the workers spec for what the classifier needs.
