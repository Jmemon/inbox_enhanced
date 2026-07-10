import { useCallback, useEffect, useRef, useState } from 'react'
import {
  approveAction, getActivity, getReviews, rejectAction, undoAction,
  type ActionFeedItem,
} from '../../lib/api'
import { subscribeSse } from '../../lib/sse'
import { ActionActivityLine, ActionCard } from '../../actions/ActionCard'

const BUSY_TIMEOUT_MS = 10_000
const UNDOABLE_ACTION_TYPES = new Set<ActionFeedItem['action_type']>(['archive_thread', 'label_thread'])

function agoLabel(epochSecs: number): string {
  if (Number.isNaN(epochSecs)) return 'unknown'
  const s = Math.max(0, Math.floor(Date.now() / 1000) - epochSecs)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

function createdAgo(iso: string): string {
  return agoLabel(Math.floor(Date.parse(iso) / 1000))
}

// This task's own actions panel (Phase 5 Task 6), rendered as ReviewFeed's
// sibling in TaskDetail.tsx rather than folded into it — ReviewFeed's props
// are locked to the TaskEvent contract (events/entitiesById/onApprove/
// onReject/onRevert) and widening that union isn't worth it for a type of
// item ReviewFeed was never built to render. Instead this takes the same
// self-owned-fetch posture as the HUD's ReviewTray/ActivityTicker: it calls
// the cross-task getReviews()/getActivity() endpoints itself and filters
// down to this one task_id — neither endpoint takes a task_id filter
// server-side, so this is the simplest correct source (mirrors those two
// HUD components' own "no other consumer, so no shared store" rationale).
//
// Pending cards (approve/reject) and settled activity lines (undo) are two
// sections of ONE component/fetch rather than two components, since both
// need the identical taskId-scoped getReviews()/getActivity() calls and
// action_updated subscription — splitting them would just double the
// fetch/SSE wiring for no benefit.
export function TaskActionsPanel({ taskId }: { taskId: string }) {
  const [pending, setPending] = useState<ActionFeedItem[]>([])
  const [activity, setActivity] = useState<ActionFeedItem[]>([])

  const refetch = useCallback(async () => {
    try {
      const [reviews, act] = await Promise.all([getReviews(), getActivity()])
      setPending(reviews.filter((i): i is ActionFeedItem => i.type === 'action' && i.task_id === taskId))
      setActivity(act.filter((i): i is ActionFeedItem => i.type === 'action' && i.task_id === taskId))
    } catch (e) {
      console.error('[TaskActionsPanel] refetch failed', e)
    }
  }, [taskId])

  useEffect(() => { void refetch() }, [refetch])

  useEffect(() => {
    return subscribeSse((e) => {
      if (e.event === '_open') {
        void refetch()
        return
      }
      // Pure nudge, scoped to this task — every other action-affecting route
      // (approve/reject/undo) publishes `action_updated {task_id}` directly
      // rather than through _publish_task_updated (see api/actions.py's
      // module docstring), so this is the only signal this panel has for a
      // change made from elsewhere (another tab, or the HUD's own
      // ReviewTray/ActivityTicker acting on this same task's items).
      if (e.event === 'action_updated' && e.task_id === taskId) void refetch()
    })
  }, [refetch, taskId])

  // --- pending cards: busy + failed-override, mirrors ReviewTray ---
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const busyTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const [failedOverrides, setFailedOverrides] = useState<Map<string, { item: ActionFeedItem; error: string }>>(new Map())

  useEffect(() => {
    const stillPending = new Set(pending.map((i) => i.action_id))
    setBusy((prev) => {
      let changed = false
      const next = new Set<string>()
      for (const id of prev) {
        if (stillPending.has(id)) {
          next.add(id)
        } else {
          const timer = busyTimersRef.current.get(id)
          if (timer) {
            clearTimeout(timer)
            busyTimersRef.current.delete(id)
          }
          changed = true
        }
      }
      return changed ? next : prev
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending])

  useEffect(() => {
    return () => {
      for (const timer of busyTimersRef.current.values()) clearTimeout(timer)
      busyTimersRef.current.clear()
    }
  }, [])

  const markBusy = (id: string) => {
    setBusy((prev) => new Set(prev).add(id))
    const timer = setTimeout(() => {
      setBusy((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next.size === prev.size ? prev : next
      })
      busyTimersRef.current.delete(id)
    }, BUSY_TIMEOUT_MS)
    busyTimersRef.current.set(id, timer)
  }

  const handleApprove = (item: ActionFeedItem) => {
    markBusy(item.action_id)
    void approveAction(item.action_id)
      .then((result) => {
        if (result.status === 'failed') {
          setFailedOverrides((prev) => new Map(prev).set(item.action_id, { item, error: result.error ?? 'action failed' }))
        }
        return refetch()
      })
      .catch((e) => console.error('[TaskActionsPanel] approve failed', e))
  }

  const handleReject = (item: ActionFeedItem) => {
    markBusy(item.action_id)
    void rejectAction(item.action_id)
      .then(() => refetch())
      .catch((e) => console.error('[TaskActionsPanel] reject failed', e))
  }

  // --- activity lines: undo busy + inline error, mirrors ActivityTicker ---
  const [undoBusy, setUndoBusy] = useState<Set<string>>(new Set())
  const undoBusyTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const [undoErrors, setUndoErrors] = useState<Map<string, string>>(new Map())

  useEffect(() => {
    const stillUndoable = new Set(activity.filter((a) => a.status === 'executed').map((a) => a.action_id))
    setUndoBusy((prev) => {
      let changed = false
      const next = new Set<string>()
      for (const id of prev) {
        if (stillUndoable.has(id)) {
          next.add(id)
        } else {
          const timer = undoBusyTimersRef.current.get(id)
          if (timer) {
            clearTimeout(timer)
            undoBusyTimersRef.current.delete(id)
          }
          changed = true
        }
      }
      return changed ? next : prev
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activity])

  useEffect(() => {
    return () => {
      for (const timer of undoBusyTimersRef.current.values()) clearTimeout(timer)
      undoBusyTimersRef.current.clear()
    }
  }, [])

  const markUndoBusy = (id: string) => {
    setUndoBusy((prev) => new Set(prev).add(id))
    const timer = setTimeout(() => {
      setUndoBusy((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next.size === prev.size ? prev : next
      })
      undoBusyTimersRef.current.delete(id)
    }, BUSY_TIMEOUT_MS)
    undoBusyTimersRef.current.set(id, timer)
  }

  const handleUndo = (item: ActionFeedItem) => {
    markUndoBusy(item.action_id)
    setUndoErrors((prev) => {
      if (!prev.has(item.action_id)) return prev
      const next = new Map(prev)
      next.delete(item.action_id)
      return next
    })
    void undoAction(item.action_id)
      .then(() => refetch())
      .catch((e: any) => {
        setUndoErrors((prev) => new Map(prev).set(item.action_id, e?.message ?? 'undo failed'))
      })
  }

  const failedOverrideList = Array.from(failedOverrides.values())
  // Nothing to show: no rules have ever fired for this task. RulesSection
  // (rendered below, on the task page) already covers the "no rules yet"
  // empty state, so this panel simply doesn't render rather than adding a
  // second, redundant "nothing here" box under it.
  if (pending.length === 0 && failedOverrideList.length === 0 && activity.length === 0) return null

  return (
    <div style={{ border: '1px solid #eee', borderRadius: 8, padding: 16, display: 'grid', gap: 16, alignContent: 'start' }}>
      {(pending.length > 0 || failedOverrideList.length > 0) && (
        <div>
          <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
            Actions needing review ({pending.length + failedOverrideList.length})
          </div>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
            {pending.map((item) => (
              <ActionCard
                key={item.action_id}
                item={item}
                busy={busy.has(item.action_id)}
                errorText={null}
                onApprove={() => handleApprove(item)}
                onReject={() => handleReject(item)}
                showTaskLink={false}
              />
            ))}
            {failedOverrideList.map(({ item, error }) => (
              <ActionCard
                key={`failed-${item.action_id}`}
                item={item}
                busy={false}
                errorText={error}
                onApprove={() => {}}
                onReject={() => {}}
                showTaskLink={false}
              />
            ))}
          </ul>
        </div>
      )}
      {activity.length > 0 && (
        <div>
          <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>Action activity</div>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
            {activity.map((item) => {
              const canUndo = item.status === 'executed' && UNDOABLE_ACTION_TYPES.has(item.action_type)
              return (
                <ActionActivityLine
                  key={item.action_id}
                  item={item}
                  agoText={createdAgo(item.created_at)}
                  showTaskLink={false}
                  busy={undoBusy.has(item.action_id)}
                  undoErrorText={undoErrors.get(item.action_id) ?? null}
                  onUndo={canUndo ? () => handleUndo(item) : null}
                />
              )
            })}
          </ul>
        </div>
      )}
    </div>
  )
}
