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
  | { event: 'bucket_draft_preview'; draft_id: string; positives: PreviewExample[]; near_misses: PreviewExample[] }
  | { event: 'extend_complete'; thread_ids: string[]; more: boolean }

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
      _reconnectTimer = setTimeout(_open, delay)
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
