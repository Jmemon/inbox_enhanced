

## inbox data model
### postgres
```python
class InboxThread(Base):
    __tablename__ = "inbox_threads"
    __table_args__ = (UniqueConstraint("user_id", "gmail_id",name="uq_inbox_threads_user_gmail"),)

    id: str  
    user_id: str (FK users.id)
    gmail_id: str
    subject: str | None
    bucket_id: str | None (FK buckets.id)
    recent_message_id: str | None 

class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (UniqueConstraint("user_id", "gmail_id", name="uq_inbox_messages_user_gmail"),)

    id: str
    thread_id: str (FK inbox_threads.id)
    user_id: str (FK users.id)
    gmail_id: str
    gmail_thread_id: str
    gmail_internal_date: int (BigInt, indexed) 
    gmail_history_id: str
    to_addr: str | None
    from_addr: str | None
    body_preview: str | None

class User: 
    id, email, name,
    gmail_refresh_token (encrypted), gmail_access_token (encrypted),
    gmail_access_token_expires_at, gmail_last_history_id, created_at

class Bucket: 
    id, user_id (null = global default),
    name, criteria, is_deleted 
```

### client-side
```ts
inbox = [
  {
    "id": "<uuid-hex>",
    "gmail_thread_id": "<gmail id>",
    "subject": "...",
    "bucket_id": "<uuid-hex> | null",
    "recent_message": {
      "id": "<uuid-hex>",
      "gmail_message_id": "<gmail id>",
      "internal_date": 1730000000000,
      "from": "...",
      "to": "...",
      "body_preview": "..."
    }
  }
]
```

## Postgres
add tracking for the email thread blobs



## Blob
Add a blob storage service where we will keep email bodies + attachments


