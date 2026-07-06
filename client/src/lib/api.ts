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
