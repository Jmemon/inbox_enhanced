import type { PreviewExample } from './sse'

export type AuthError = 'unauthorized' | 'network'

// Generic JSON fetch with timing and logging so every API call is visible in DevTools.
export async function getJSON<T>(url: string): Promise<T> {
  const t0 = performance.now()
  console.log('[api] GET', url)
  const r = await fetch(url, { credentials: 'same-origin' })
  const ms = Math.round(performance.now() - t0)
  if (r.status === 401) {
    console.error('[api] GET', url, '→ 401 unauthorized', ms, 'ms')
    throw { kind: 'unauthorized' as const }
  }
  if (!r.ok) {
    console.error('[api] GET', url, '→', r.status, r.statusText, ms, 'ms')
    throw new Error(`${r.status} ${r.statusText}`)
  }
  console.log('[api] GET', url, '→', r.status, ms, 'ms')
  return (await r.json()) as T
}

export async function postEmpty(url: string): Promise<void> {
  const t0 = performance.now()
  console.log('[api] POST', url)
  const r = await fetch(url, { method: 'POST', credentials: 'same-origin' })
  const ms = Math.round(performance.now() - t0)
  if (!r.ok && r.status !== 204) {
    console.error('[api] POST', url, '→', r.status, r.statusText, ms, 'ms')
    throw new Error(`${r.status} ${r.statusText}`)
  }
  console.log('[api] POST', url, '→', r.status, ms, 'ms')
}

// --- Inbox types ---

export type InboxMessage = {
  id: string
  gmail_message_id: string
  internal_date: number
  from: string | null
  to: string | null
  body_preview: string | null
  is_unread?: boolean
}

export type InboxThread = {
  id: string
  gmail_thread_id: string
  subject: string | null
  bucket_id: string | null
  recent_message: InboxMessage | null
  is_archived: boolean
}

export type InboxPage = {
  as_of: number
  page: number
  limit: number
  threads: InboxThread[]
}

export function getInbox(opts: { page?: number; limit?: number } = {}): Promise<InboxPage> {
  const params = new URLSearchParams()
  if (opts.page) params.set('page', String(opts.page))
  if (opts.limit) params.set('limit', String(opts.limit))
  const qs = params.toString()
  const url = `/api/inbox${qs ? `?${qs}` : ''}`
  const t0 = performance.now()
  console.log('[api] getInbox', opts)
  return getJSON<InboxPage>(url).then((r) => {
    console.log('[api] getInbox →', r.threads.length, 'threads in', Math.round(performance.now() - t0), 'ms')
    return r
  }).catch((e) => {
    console.error('[api] getInbox failed', e)
    throw e
  })
}

export function searchInbox(q: string): Promise<InboxPage> {
  const params = new URLSearchParams({ q })
  return getJSON<InboxPage>(`/api/search?${params.toString()}`)
}

export function getThread(id: string): Promise<InboxThread> {
  const url = `/api/threads/${encodeURIComponent(id)}`
  const t0 = performance.now()
  console.log('[api] getThread', id)
  return getJSON<InboxThread>(url).then((r) => {
    console.log('[api] getThread', id, '→ found in', Math.round(performance.now() - t0), 'ms')
    return r
  }).catch((e) => {
    console.error('[api] getThread', id, 'failed', e)
    throw e
  })
}

export async function getThreadsBatch(thread_ids: string[]): Promise<InboxThread[]> {
  // One round trip for N ids. Used by the SSE replay path: a single SSE event
  // can carry up to ~200 thread ids on a kickoff full sync, and N parallel
  // GET /api/threads/{id} calls would create avoidable connection churn.
  if (thread_ids.length === 0) return []
  const t0 = performance.now()
  console.log('[api] getThreadsBatch requested', thread_ids.length, 'ids')
  const r = await fetch('/api/threads/batch', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ thread_ids }),
  })
  const ms = Math.round(performance.now() - t0)
  if (r.status === 401) {
    console.error('[api] getThreadsBatch → 401 unauthorized', ms, 'ms')
    throw { kind: 'unauthorized' as const }
  }
  if (!r.ok) {
    console.error('[api] getThreadsBatch → ', r.status, r.statusText, ms, 'ms')
    throw new Error(`${r.status} ${r.statusText}`)
  }
  const body = (await r.json()) as { threads: InboxThread[] }
  console.log('[api] getThreadsBatch →', body.threads.length, 'threads returned in', ms, 'ms')
  return body.threads
}

export async function requestRefresh(): Promise<void> {
  const t0 = performance.now()
  console.log('[api] requestRefresh POST /api/inbox/refresh')
  const r = await fetch('/api/inbox/refresh', { method: 'POST', credentials: 'same-origin' })
  const ms = Math.round(performance.now() - t0)
  if (r.status !== 202 && r.status !== 200) {
    console.error('[api] requestRefresh → ', r.status, ms, 'ms')
    throw new Error(`refresh failed: ${r.status}`)
  }
  console.log('[api] requestRefresh → 202 in', ms, 'ms')
}

// --- Bucket types ---
export type Bucket = { id: string; name: string; criteria: string; is_default: boolean }
export type BucketExampleIn = { sender: string; subject: string; snippet: string; rationale: string }

// --- Bucket calls ---
export function getBuckets(): Promise<{ buckets: Bucket[] }> {
  return getJSON<{ buckets: Bucket[] }>('/api/buckets')
}

export async function createBucket(body: {
  name: string; description: string;
  confirmed_positives: BucketExampleIn[]; confirmed_negatives: BucketExampleIn[];
}): Promise<Bucket> {
  const r = await fetch('/api/buckets', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`create bucket: ${r.status}`)
  return r.json()
}

export async function patchBucket(id: string, name: string): Promise<Bucket> {
  const r = await fetch(`/api/buckets/${encodeURIComponent(id)}`, {
    method: 'PATCH', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name }),
  })
  if (!r.ok) throw new Error(`rename bucket: ${r.status}`)
  return r.json()
}

export async function deleteBucket(id: string): Promise<void> {
  const r = await fetch(`/api/buckets/${encodeURIComponent(id)}`, {
    method: 'DELETE', credentials: 'same-origin',
  })
  if (r.status !== 204) throw new Error(`delete bucket: ${r.status}`)
}

export async function postBucketDraftPreview(body: {
  name: string; description: string; exclude_thread_ids?: string[];
}): Promise<{ draft_id: string }> {
  const r = await fetch('/api/buckets/draft/preview', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ exclude_thread_ids: [], ...body }),
  })
  if (r.status !== 202) throw new Error(`draft preview: ${r.status}`)
  return r.json()
}

export type DraftPreviewPoll =
  | { status: 'pending' }
  | { status: 'ready'; positives: PreviewExample[]; near_misses: PreviewExample[] }
  | { status: 'gone' }   // 404: TTL expired or unknown draft_id

// Polling fallback for the SSE-pushed bucket_draft_preview event. The worker
// caches its result in redis with a 600s TTL keyed on draft_id; this hits
// that cache directly so a lost SSE frame doesn't strand the modal.
export async function getBucketDraftPreview(draft_id: string): Promise<DraftPreviewPoll> {
  const url = `/api/buckets/draft/preview/${encodeURIComponent(draft_id)}`
  const t0 = performance.now()
  const r = await fetch(url, { credentials: 'same-origin' })
  const ms = Math.round(performance.now() - t0)
  if (r.status === 202) {
    console.log('[api] poll', url, '→ pending', ms, 'ms')
    return { status: 'pending' }
  }
  if (r.status === 200) {
    const body = await r.json() as {
      status: 'ready'; positives: PreviewExample[]; near_misses: PreviewExample[]
    }
    console.log('[api] poll', url, '→ ready (+',
                body.positives.length, 'positives, +',
                body.near_misses.length, 'near-misses) in', ms, 'ms')
    return body
  }
  if (r.status === 404) {
    console.warn('[api] poll', url, '→ 404 (gone) in', ms, 'ms')
    return { status: 'gone' }
  }
  console.error('[api] poll', url, '→', r.status, ms, 'ms')
  throw new Error(`draft preview poll: ${r.status}`)
}

export async function postInboxExtend(beforeInternalDate: number): Promise<void> {
  const r = await fetch('/api/inbox/extend', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ before_internal_date: beforeInternalDate }),
  })
  if (r.status !== 202) throw new Error(`extend: ${r.status}`)
}

// --- Sync status ---

export type SyncStatus = { last_synced_at: number | null; has_cursor: boolean }

export function getSyncStatus(): Promise<SyncStatus> {
  return getJSON<SyncStatus>('/api/sync/status')
}

// --- Task types ---
// Mirrors server/app/api/tasks.py's `_serialize_*` helpers field-for-field —
// see that module's docstring for the extraction pipeline these feed.

export type TaskSummary = { entities: number; pending_reviews: number; last_event_at: string | null }
export type Task = { id: string; name: string; goal: string; kind: string; status: 'active' | 'paused'; version: number; summary: TaskSummary }
export type TaskStateSchema = {
  version: number
  entity: { noun: string; identity_hint: string; attributes: { key: string; type: string; values?: string[] | null }[] } | null
  pipeline: { stages: string[]; terminal: string[] }
}
// criteria is detail-only (mirrors server's _serialize_task_detail, which
// omits it from the list-item serializer) — the spec §4.6 learning loop
// grows this text with every attach/detach example, and exposing it here
// is what makes that growth auditable from a task's own detail page.
export type TaskDetail = Task & { state_schema: TaskStateSchema | null; criteria: string }
export type TaskEntity = { id: string; entity_key: string; display_name: string; state: Record<string, string | null>; updated_at: string }
export type TaskEvent = {
  id: string; field: string | null; old_value: string | null; new_value: string | null
  evidence_quote: string | null; confidence: number | null; origin: 'llm' | 'user'
  status: 'applied' | 'pending_review' | 'rejected' | 'reverted'
  pending_reason: string | null; proposed_entity: string | null
  thread_id: string | null; message_id: string | null; gmail_message_id: string | null
  entity_id: string | null; created_at: string
}
export type TaskDraftProposal = { name: string; description: string; state_schema: TaskStateSchema; keyword_probes: string[] }
export type TaskDraftPoll =
  | { status: 'pending' } | { status: 'gone' }
  | { status: 'ready'; proposal: TaskDraftProposal; positives: PreviewExample[]; near_misses: PreviewExample[] }

// --- Task calls ---

export async function postTaskDraft(goal: string): Promise<{ draft_id: string }> {
  const r = await fetch('/api/tasks/draft', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ goal }),
  })
  if (r.status !== 202) throw new Error(`task draft: ${r.status}`)
  return r.json()
}

// Polling fallback for the SSE-pushed task_draft_ready event — same
// 200/202/404 mapping as getBucketDraftPreview (GET /api/tasks/draft/{id}
// can also 403 "not your draft" like its bucket counterpart; that falls
// through to the generic throw below, same as getBucketDraftPreview does).
export async function getTaskDraft(draftId: string): Promise<TaskDraftPoll> {
  const url = `/api/tasks/draft/${encodeURIComponent(draftId)}`
  const t0 = performance.now()
  const r = await fetch(url, { credentials: 'same-origin' })
  const ms = Math.round(performance.now() - t0)
  if (r.status === 202) {
    console.log('[api] poll', url, '→ pending', ms, 'ms')
    return { status: 'pending' }
  }
  if (r.status === 200) {
    const body = (await r.json()) as TaskDraftPoll
    console.log('[api] poll', url, '→ ready in', ms, 'ms')
    return body
  }
  if (r.status === 404) {
    console.warn('[api] poll', url, '→ 404 (gone) in', ms, 'ms')
    return { status: 'gone' }
  }
  console.error('[api] poll', url, '→', r.status, ms, 'ms')
  throw new Error(`task draft poll: ${r.status}`)
}

// Helper to extract actionable error detail from API responses (e.g., pydantic 422 validators).
// If the response JSON contains a string `detail` field, use it; otherwise fall back to the
// provided fallback message. A LIST detail (e.g., from pydantic array validation) uses the fallback.
async function throwWithDetail(r: Response, fallback: string): Promise<never> {
  let message = fallback
  try {
    const errBody = await r.json()
    if (typeof errBody?.detail === 'string') message = errBody.detail
  } catch {
    // not JSON — keep the fallback
  }
  throw new Error(message)
}

export async function createTask(body: {
  name: string; goal: string; description: string; state_schema: TaskStateSchema;
  keyword_probes: string[]; confirmed_positives: BucketExampleIn[]; confirmed_negatives: BucketExampleIn[];
}): Promise<TaskDetail> {
  // POST /api/tasks 201s with the full task-detail body (_serialize_task_detail
  // — state_schema + summary included), not the bare Task shape.
  const r = await fetch('/api/tasks', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) {
    await throwWithDetail(r, `create task: ${r.status}`)
  }
  return r.json()
}

export function getTasks(): Promise<{ tasks: Task[] }> {
  return getJSON<{ tasks: Task[] }>('/api/tasks')
}

export function getTask(id: string): Promise<TaskDetail> {
  return getJSON<TaskDetail>(`/api/tasks/${encodeURIComponent(id)}`)
}

export async function patchTask(id: string, body: {
  name?: string; status?: 'active' | 'paused'; state_schema?: TaskStateSchema;
}): Promise<TaskDetail> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}`, {
    method: 'PATCH', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`patch task: ${r.status}`)
  return r.json()
}

export async function deleteTask(id: string): Promise<void> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}`, {
    method: 'DELETE', credentials: 'same-origin',
  })
  if (r.status !== 204) throw new Error(`delete task: ${r.status}`)
}

export function getTaskBoard(id: string): Promise<{ entities: TaskEntity[] }> {
  return getJSON<{ entities: TaskEntity[] }>(`/api/tasks/${encodeURIComponent(id)}/board`)
}

export function getTaskEvents(id: string, opts: {
  status?: TaskEvent['status']; entity_id?: string; page?: number; limit?: number;
} = {}): Promise<{ events: TaskEvent[] }> {
  const params = new URLSearchParams()
  if (opts.status) params.set('status', opts.status)
  if (opts.entity_id) params.set('entity_id', opts.entity_id)
  if (opts.page) params.set('page', String(opts.page))
  if (opts.limit) params.set('limit', String(opts.limit))
  const qs = params.toString()
  return getJSON<{ events: TaskEvent[] }>(`/api/tasks/${encodeURIComponent(id)}/events${qs ? `?${qs}` : ''}`)
}

export function getTaskThreads(id: string): Promise<{ threads: InboxThread[] }> {
  return getJSON<{ threads: InboxThread[] }>(`/api/tasks/${encodeURIComponent(id)}/threads`)
}

// POST /api/tasks/{id}/threads 201s with the attached thread's serialized
// InboxThread body — discarded here (fire-and-forget; callers already hold
// the thread client-side and get the fresh state via task_updated + refetch).
export async function attachThread(id: string, threadId: string, addExample: boolean): Promise<void> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/threads`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, add_example: addExample }),
  })
  if (r.status !== 201) throw new Error(`attach thread: ${r.status}`)
}

export async function detachThread(id: string, threadId: string, addExample: boolean): Promise<void> {
  const params = new URLSearchParams({ add_example: String(addExample) })
  const r = await fetch(
    `/api/tasks/${encodeURIComponent(id)}/threads/${encodeURIComponent(threadId)}?${params.toString()}`,
    { method: 'DELETE', credentials: 'same-origin' },
  )
  if (r.status !== 204) throw new Error(`detach thread: ${r.status}`)
}

// approve/reject/revert all return 200 (no status_code override) with the
// serialized TaskEvent body — discarded here, same rationale as attachThread;
// the review tray refetches events off the task_updated SSE push.
export async function approveEvent(id: string, eventId: string): Promise<void> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/events/${encodeURIComponent(eventId)}/approve`, {
    method: 'POST', credentials: 'same-origin',
  })
  if (!r.ok) throw new Error(`approve event: ${r.status}`)
}

export async function rejectEvent(id: string, eventId: string): Promise<void> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/events/${encodeURIComponent(eventId)}/reject`, {
    method: 'POST', credentials: 'same-origin',
  })
  if (!r.ok) throw new Error(`reject event: ${r.status}`)
}

export async function revertEvent(id: string, eventId: string): Promise<void> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/events/${encodeURIComponent(eventId)}/revert`, {
    method: 'POST', credentials: 'same-origin',
  })
  if (!r.ok) throw new Error(`revert event: ${r.status}`)
}

export async function setEntityState(id: string, entityId: string, field: string, value: string): Promise<void> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/entities/${encodeURIComponent(entityId)}/state`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ field, value }),
  })
  if (!r.ok) {
    await throwWithDetail(r, `set entity state: ${r.status}`)
  }
}

export async function mergeEntity(id: string, entityId: string, intoEntityId: string): Promise<void> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/entities/${encodeURIComponent(entityId)}/merge`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ into_entity_id: intoEntityId }),
  })
  if (r.status !== 204) {
    await throwWithDetail(r, `merge entity: ${r.status}`)
  }
}
