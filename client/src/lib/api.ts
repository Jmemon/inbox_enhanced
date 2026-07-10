import type { PreviewExample } from './sse'

export type AuthError = 'unauthorized' | 'network'

// Structured HTTP-error type carrying the real status code, so callers can
// branch on `e.status` (e.g. 404) instead of substring-matching `e.message`
// against a `${status} ${statusText}` string — a proxy that echoes an
// upstream status code into its own statusText could otherwise false-positive
// a plain-Error substring check. Deliberately NOT used for 401s (see getJSON
// below): useAuth/recheckSession depend on the exact `{kind:'unauthorized'}`
// shape, not on ApiError/instanceof Error.
export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

// Generic JSON fetch with timing and logging so every API call is visible in DevTools.
export async function getJSON<T>(url: string): Promise<T> {
  const t0 = performance.now()
  console.log('[api] GET', url)
  const r = await fetch(url, { credentials: 'same-origin' })
  const ms = Math.round(performance.now() - t0)
  if (r.status === 401) {
    console.error('[api] GET', url, '→ 401 unauthorized', ms, 'ms')
    // KEEP this exact shape — useAuth's refresh()/recheckSession() key off
    // `e.kind === 'unauthorized'`, not instanceof, to distinguish a
    // definitive 401 from any other failure (network/5xx/timeout).
    throw { kind: 'unauthorized' as const }
  }
  if (!r.ok) {
    console.error('[api] GET', url, '→', r.status, r.statusText, ms, 'ms')
    throw new ApiError(`${r.status} ${r.statusText}`, r.status)
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
// Bucket is now the client-side mapped shape of a `GET /api/tasks?kind=bucket`
// list item (buckets are tasks(kind='bucket'), see reference/); BucketExampleIn
// is still reused as the confirmed_positives/negatives element for createTask.
export type Bucket = { id: string; name: string; criteria: string; is_default: boolean }
export type BucketExampleIn = { sender: string; subject: string; snippet: string; rationale: string }

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

export type TaskSummary = {
  entities: number; pending_reviews: number; last_event_at: string | null
  // Histogram of entities.state["stage"] — key order is server-controlled
  // (schema stage order, then observed extras, then "(no stage)" last).
  // Consumers must preserve insertion order (plain Object.entries), not sort.
  stage_counts: Record<string, number>
}
// criteria/is_default are kind-conditional extras _serialize_task_list_item
// only adds for kind='bucket' rows (GET /api/tasks?kind=bucket) — absent on
// tracker rows, hence optional here.
export type Task = {
  id: string; name: string; goal: string; kind: string; status: 'active' | 'paused'; version: number
  summary: TaskSummary; criteria?: string; is_default?: boolean
}
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
// Cross-task feed item: a TaskEvent plus the fields /api/reviews and
// /api/activity add so the HUD can route back to the owning task without a
// second fetch (see server/app/api/tasks.py's _serialize_feed_event).
export type FeedItem = TaskEvent & {
  task_id: string
  task_name: string
  entity_display_name: string | null
}
// --- Task calls ---

// Helper to extract actionable error detail from API responses (e.g., pydantic 422 validators).
// If the response JSON contains a string `detail` field, use it; otherwise fall back to the
// provided fallback message. A LIST detail (e.g., from pydantic array validation) uses the fallback.
// Throws ApiError (status preserved) rather than a bare Error — its three call
// sites (createTask, setEntityState, mergeEntity) only ever render `e.message`,
// and `ApiError instanceof Error` still holds, so this is a transparent upgrade.
async function throwWithDetail(r: Response, fallback: string): Promise<never> {
  let message = fallback
  try {
    const errBody = await r.json()
    if (typeof errBody?.detail === 'string') message = errBody.detail
  } catch {
    // not JSON — keep the fallback
  }
  throw new ApiError(message, r.status)
}

export async function createTask(body: {
  name: string; goal: string; description: string; state_schema: TaskStateSchema | null;
  kind?: 'tracker' | 'bucket';
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

// kind='bucket' -> GET /api/tasks?kind=bucket, the task-backed replacement
// for the old dedicated bucket list (defaults included). No-arg call sites
// (the tracker HUD grid) are unchanged: `kind` omitted -> tracker-only.
export function getTasks(opts: { kind?: 'bucket' } = {}): Promise<{ tasks: Task[] }> {
  const qs = opts.kind ? `?kind=${opts.kind}` : ''
  return getJSON<{ tasks: Task[] }>(`/api/tasks${qs}`)
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

// Unified review tray: every still-pending_review event across all of this
// user's tasks, newest first. limit is clamped server-side to [1, 200].
export function getReviews(limit?: number): Promise<FeedItem[]> {
  const qs = limit ? `?limit=${limit}` : ''
  return getJSON<{ reviews: FeedItem[] }>(`/api/reviews${qs}`).then(r => r.reviews)
}

// Activity ticker: every non-pending_review event across all of this user's
// tasks, newest first. limit is clamped server-side to [1, 100].
export function getActivity(limit?: number): Promise<FeedItem[]> {
  const qs = limit ? `?limit=${limit}` : ''
  return getJSON<{ activity: FeedItem[] }>(`/api/activity${qs}`).then(r => r.activity)
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

// --- Job types + calls (Phase 4.5, spec 005) ---
// Mirrors server/app/api/jobs.py's `_serialize_job` field-for-field. Jobs
// are the persisted, pollable replacement for the old fire-and-forget
// `task_draft_ready` popup (postTaskDraft/getTaskDraft above, deleted) — see
// state/JobsProvider.tsx for the store that owns fetching/polling these.

// `payload` is written by the propose worker for 'creation' jobs only and is
// NESTED (proposal fields separate from the confirmed examples) — NOT a
// flat merge of TaskDraftProposal + examples, and there is deliberately no
// `criteria` key. 'delete_retriage' jobs never set it (stays null).
export type JobPayload = {
  proposal: { name: string; description: string; state_schema: TaskStateSchema | null; keyword_probes: string[] }
  positives: PreviewExample[]
  near_misses: PreviewExample[]
}

export type Job = {
  id: string
  kind: 'creation' | 'delete_retriage'
  // null for 'delete_retriage' jobs (task_kind is a 'creation'-only column).
  task_kind: 'tracker' | 'bucket' | null
  // Superset of both kind's stage machines (spec §1.2): 'creation' moves
  // proposing -> draft_ready -> backfilling -> done|failed; 'delete_retriage'
  // moves running -> done|failed. The literal string 'dismissed' never
  // appears here — dismissal is recorded only via `dismissed_at` below.
  stage: 'proposing' | 'draft_ready' | 'backfilling' | 'running' | 'done' | 'failed'
  needs_user: boolean
  payload: JobPayload | null
  task_id: string | null
  goal: string
  scanned: number
  matched: number
  total: number
  error: string | null
  created_at: string
  updated_at: string
  dismissed_at: string | null
}

// POST /api/jobs 202s with the newly created (stage='proposing') job row —
// starts the goal->draft flow the propose worker fills in asynchronously.
export async function createJob(goal: string, taskKind: 'tracker' | 'bucket'): Promise<Job> {
  const r = await fetch('/api/jobs', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ goal, task_kind: taskKind }),
  })
  if (r.status !== 202) {
    await throwWithDetail(r, `create job: ${r.status}`)
  }
  const body = (await r.json()) as { job: Job }
  return body.job
}

// active=true (the default, matching the server's own `active: int = 1`
// default) is the panel's normal view; pass false for every non-dismissed
// job regardless of age.
export function getJobs(active = true): Promise<Job[]> {
  const qs = active ? '' : '?active=0'
  return getJSON<{ jobs: Job[] }>(`/api/jobs${qs}`).then(r => r.jobs)
}

export function getJob(id: string): Promise<Job> {
  return getJSON<{ job: Job }>(`/api/jobs/${encodeURIComponent(id)}`).then(r => r.job)
}

// Mirrors server's `_ConfirmJobBody` — every `_CreateTaskBody` field except
// `kind` (fixed at POST /api/jobs time as the job's own `task_kind`) and
// `goal` (already stored on the job row; the review step doesn't retype it).
export type ConfirmJobBody = {
  name: string
  description: string
  state_schema: TaskStateSchema | null
  keyword_probes: string[]
  confirmed_positives: BucketExampleIn[]
  confirmed_negatives: BucketExampleIn[]
}

// Only legal from stage='draft_ready' (409 otherwise, surfaced via
// throwWithDetail same as createTask's 422s). 200s with both the newly
// created task (full detail body) and the job's own post-confirm row
// (task_id set, stage='backfilling').
export async function confirmJob(id: string, body: ConfirmJobBody): Promise<{ task: TaskDetail; job: Job }> {
  const r = await fetch(`/api/jobs/${encodeURIComponent(id)}/confirm`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) {
    await throwWithDetail(r, `confirm job: ${r.status}`)
  }
  return r.json()
}

// Idempotent — a second dismiss on an already-dismissed job still 204s.
export async function dismissJob(id: string): Promise<void> {
  const r = await fetch(`/api/jobs/${encodeURIComponent(id)}/dismiss`, {
    method: 'POST', credentials: 'same-origin',
  })
  if (r.status !== 204) throw new Error(`dismiss job: ${r.status}`)
}
