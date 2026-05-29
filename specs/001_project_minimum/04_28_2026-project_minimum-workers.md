

> workers reach gmail using the per-user oauth tokens stored on users (defined
> in the auth spec); refresh-on-demand goes through the same helper as the api.

> sync flow (poll_new_messages, partial_sync_inbox, message parser, pub/sub
> channels, beat scheduler) moved to specs/04_30_2026-project_minimum-homepage.md.

## worker: full_sync_inbox
input: userId.
that's enough to call the google api to get the threads using the user's
stored oauth token.

pull down most recently active 200 threads
parse the threads (message parser + thread assembler) into a string
representation
 - include headers, bodies, and attachments
run each through classification pipeline
if user has threads in postgres, lastHistoryId was old so had to do full sync:
 - easy option: throw out what was in there and proceed to the "user has no
   threads in postgres" case.
 - more challenging option: reconcile. would have to account for messages
   deleted and added. naively imagining reconstructing the history records
   from a diff between newest 200 most recent and whatever is in the database
   as the 200 most recently active threads.
 - let's do easy for this.
if user has no threads in postgres:
 - write rows for every thread and message into inbox_messages and inbox_threads
 - update users.gmail_lastHistoryId with most recent message's historyId

publish done notification to pub/sub channel for job.


## classification function
classify threads given threadstrings and bucket criteria.

### Default buckets
Important
Can wait
Auto-archive
Newsletter

### llm-powered classification pipeline
- factorable-criteria to support custom buckets / arbitrary class criteria.
- is it one call per class? or all classes in each call? or all classes in
  each call until too many classes, then select topK classes to send as
  options to classification call? final option.
- multiple classes for an email? no.

### custom buckets
users can specify new buckets that the classification pipeline can recognize.
how? user describes the bucket a bit, maybe interactive back-and-forth to
build bucket by surfacing old emails and asking if they belong, or if not
viable (user knows no old ones would fit), maybe by producing synthetic
examples that would belong in/out of bucket, having user indicate if correct
(labeling some data), or having them update examples so categorizations are
correct.
- name, criteria. criteria agreed upon at bucket creation. user can create it
  and llm can suggest updates.
- ideally is iterated on by user feedback.
