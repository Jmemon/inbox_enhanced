import type { TaskEvent } from '../../lib/api'

// Maps the task engine's five machine-readable pending_reason enum values
// (server/app/task_engine's correction gate) to user-facing copy.
// near_duplicate_entity interpolates the LLM's raw proposal; the rest are
// static. Any other value — including null, e.g. events that never hit a
// gate — omits the reason line entirely rather than leaking a raw enum
// string into the UI.
//
// Takes TaskEvent (not the narrower FeedItem) so both ReviewFeed (per-task
// events) and the HUD's ReviewTray (cross-task FeedItem, a TaskEvent
// superset) share this one map.
export function pendingReasonCopy(event: TaskEvent): string | null {
  switch (event.pending_reason) {
    case 'near_duplicate_entity':
      return `LLM proposed '${event.proposed_entity ?? '?'}' — close to an existing entity`
    case 'backward_move':
      return 'moves the pipeline backward — confirm it'
    case 'terminal_locked':
      return 'entity is in a terminal stage — only you can move it'
    case 'fence_blocked':
      return 'an older email tried to change something you corrected'
    case 'low_confidence':
      return 'low confidence — needs your call'
    default:
      return null
  }
}
