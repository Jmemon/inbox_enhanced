
## backend <> frontend api requests
client: extend-inbox
 - sent when user is trying to go to a page we dont have data for yet
server: thread-updates
 - response list of client-side thread objects

client: poll-for-inbox-updates
 - sent on timer from client side. triggers poll_for_inbox_updates job on backend
server: thread-updates
 - triggers poll_for_inbox_updates job
 - pulls down results
 - response list of client-side thread objects

client: load-N
 - load N threads before internalDate D
 - threads already in our database wont pull down from gmail api (not extend inbox)
server: thread-updates
 - response list of client-side thread objects


## backend <> gmail
load_from_zero. run on initial page open when account just created (ie when user tried to load-N but theres nothing in the database)

extend_inbox. triggered with extend inbox api endpoint

poll_for_inbox_updates. triggered within api endpoint. 

check_for_inbox_drift. runs for inactive users every 24 hours. 

