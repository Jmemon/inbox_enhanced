export type PreviewExample = {
  thread_id: string
  subject: string
  sender: string
  score: number
  rationale: string
  snippet: string
}

export type SseDataEvent =
  | { event: 'threads_updated'; thread_ids: string[] }
  | { event: 'extend_complete'; thread_ids: string[]; more: boolean }
  | { event: 'task_updated'; task_id: string; version: number; pending_count: number }
  | { event: 'task_backfill_progress'; task_id: string; scanned: number; matched: number; done: boolean }
  // Phase 4.5 Task 3: replaces `task_draft_ready` — a pure nudge (never a
  // row, per this module's own convention) telling JobsProvider to refetch
  // GET /api/jobs. Published after every job-row commit (stage changes and
  // progress ticks), so it covers both the goal->draft->backfill creation
  // flow and delete-retriage jobs.
  | { event: 'job_updated'; job_id: string }
  // Phase 5 (actions, spec 006): another pure nudge, published directly by
  // approve/reject/undo (app/api/actions.py) — NOT routed through
  // _publish_task_updated, since approving/rejecting/undoing one action
  // doesn't change task.version or pending_count in a way task_updated
  // describes. No client subscriber yet (Task 5 adds the type only; the
  // action-card surfaces that consume it land in Task 6).
  | { event: 'action_updated'; task_id: string }

export type SseConnEvent = { event: '_open' } | { event: '_error' }
export type SseEvent = SseDataEvent | SseConnEvent

let _es: EventSource | null = null
const _handlers = new Set<(e: SseEvent) => void>()

// Reconnect backoff state. There's exactly one EventSource singleton per tab,
// so plain module vars are fine. `_consecutiveErrors` resets on every
// successful `onopen` and otherwise only grows across back-to-back
// `onerror`s; `_reconnectTimer` tracks the pending reopen so `_close()`
// (explicit unsubscribe / sign-out) can cancel it — otherwise a queued
// reconnect could resurrect the EventSource seconds after the last handler
// unsubscribed.
let _consecutiveErrors = 0
let _reconnectTimer: ReturnType<typeof setTimeout> | null = null

export function subscribeSse(handler: (e: SseEvent) => void): () => void {
  _handlers.add(handler)
  if (!_es) _open()
  return () => {
    _handlers.delete(handler)
    if (_handlers.size === 0) _close()
  }
}

function _open() {
  console.log('[sse] opening EventSource')
  _es = new EventSource('/api/sse', { withCredentials: true })
  _es.onopen = () => {
    console.debug('[sse] open')
    _consecutiveErrors = 0
    for (const h of _handlers) h({ event: '_open' })
  }
  _es.onmessage = (ev) => {
    try {
      const parsed = JSON.parse(ev.data) as SseDataEvent
      if (parsed && typeof parsed === 'object' && (parsed as any).event) {
        console.debug('[sse]', parsed.event, parsed)
        for (const h of _handlers) h(parsed)
      }
    } catch {
      console.debug('[sse] malformed frame', ev.data)
    }
  }
  _es.onerror = () => {
    console.debug('[sse] error')
    _consecutiveErrors += 1
    for (const h of _handlers) h({ event: '_error' })
    _close()
    if (_handlers.size > 0) {
      // Exponential backoff (1s, 2s, 4s… capped at 30s) — the old immediate
      // queueMicrotask(_open) reopen meant an expired session turned every
      // 401 into a network-paced hammer with no backoff at all.
      const delay = Math.min(30_000, 1000 * 2 ** (_consecutiveErrors - 1))
      _reconnectTimer = setTimeout(() => {
        _reconnectTimer = null
        _open()
      }, delay)
    }
  }
}

function _close() {
  _es?.close()
  _es = null
  // Cancel any pending scheduled reopen — without this, an unsubscribe (or
  // sign-out) that lands mid-backoff would still reopen the stream later.
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer)
    _reconnectTimer = null
  }
}
