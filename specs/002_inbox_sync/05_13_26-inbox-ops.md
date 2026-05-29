
## gmail api 
users.threads
users.history
users.messages
users.messages.attachments

## backend
postgres
blob

## frontend
react

## processing threads/messages
pull down thread from gmail
parse it into our datastructure + a string repr (with attachments)
classify thread into tasks/buckets
upsert into db + blob

## websocket
do we decouple ingesting + processing. We can push down updates to client for live stuff over websocket?
maybe inbox has state machine that websocket uses to communicate. eg each thread is ingested -> processing -> up-to-date or something like that. 
when a new message is being ingested for a thread upon ingestion send down the thread with status "processing" and then once it gets classified send down classification and status update "up-to-date". 

## gmail <> backend
load_from_zero
 - clear inbox for user and pull down most recent 500 threads, processing them all. this should pretty much only be used on initial load

extend_inbox
 - pull down N threads BEFORE oldest active thread in current inbox
 - triggered. no beat. for example the client might trigger it when going onto a new page they dont have the threads for. 
 - or maybe some tasks might want to look earlier into the inboxes history for some reason and so it could be triggered there

poll_for_inbox_updates
 - checks history records and applies updates
 - runs while user is active either when they press the sync button or maybe every 30s

check_for_inbox_drift
 - checks backend repr against gmail repr. (the manual check.)
    - get oldest thread (by activity) from our backend and pull down all thread ids and message ids from gmail that datetime and after. then we cna see if we're missing anything.
    - if anything missing ingest the messages/threads and trigger processing jobs for them.
 - should be done when user is inactive since its a bit invasive. Can be manually triggered from a menu maybe
 - runs when user is offline maybe every 24 hours or something


## backend <> frontend
frontend making requests when it needs stuff. runs inbox polling timer on its end every 30s. 

Inbox:
 - threads ordered by most recently active message
 - only contains headers (can't click into them)
 - UPDATE: threads also contain statuses (ingested, processing, up-to-date). 

receive updates over the websocket. when update jobs process stuff they push results onto pub/sub or stream or whatever the websocket server is subscribed to and then pushes those updates to the corresponding active user





