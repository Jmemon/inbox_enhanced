import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { getActivity, undoAction, type ActionFeedItem, type EventFeedItem, type FeedItem } from '../../lib/api'
import { subscribeSse } from '../../lib/sse'
import { useTasksStore } from '../../state/TasksProvider'
import { ActionActivityLine } from '../../actions/ActionCard'

const BUSY_TIMEOUT_MS = 10_000
// Mirrors api/actions.py's `_UNDOABLE_ACTION_TYPES` (server is the source of
// truth — this only decides whether to SHOW the button; the server would
// 409 anyway on a stale/wrong client guess).
const UNDOABLE_ACTION_TYPES = new Set<ActionFeedItem['action_type']>(['archive_thread', 'label_thread'])

// Mirrors ReviewFeed.tsx's OriginBadge/originLabel verbatim — duplicated
// rather than shared, per this codebase's existing convention for these
// small feed-item widgets (see ReviewTray.tsx's own copied EntityLabel/
// EvidenceQuote for the identical rationale: no shared presentational
// module exists yet, and these are small enough that extracting one isn't
// worth it for a single extra consumer).
function originLabel(origin: EventFeedItem['origin']): string {
  return origin === 'llm' ? 'LLM' : 'you'
}

function OriginBadge({ origin }: { origin: EventFeedItem['origin'] }) {
  return (
    <span style={{
      display: 'inline-block', marginRight: 6, padding: '1px 6px', borderRadius: 999,
      fontSize: 10, fontWeight: 500,
      background: origin === 'llm' ? '#eef2f7' : '#e7f1ea',
      color: origin === 'llm' ? '#4b5563' : '#2f6b46',
    }}>
      {originLabel(origin)}
    </span>
  )
}

// Same entity fallback chain as ReviewTray's EntityLabel (entity_display_name
// → proposed_entity → "unknown"), collapsed to a plain string here since the
// ticker's one-line template has no room for ReviewTray's "new entity?"
// annotation.
function entityLabel(item: EventFeedItem): string {
  if (item.entity_display_name) return item.entity_display_name
  if (item.proposed_entity) return item.proposed_entity
  return 'unknown'
}

// Mirrors HudPage's agoLabel/lastEventAgo pair (epoch-seconds core + ISO
// adapter) — duplicated locally since HudPage doesn't export them, following
// the same small-helper-duplication convention as OriginBadge above.
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

// Cross-task activity ticker for the HUD. Self-owned data, same pattern as
// ReviewTray: fetches getActivity() itself (every non-pending_review event
// AND every settled action across all of the user's tasks has no other
// consumer) rather than reading from TasksProvider. Refetches on a memoized
// sum of task VERSIONS (rather than ReviewTray's summed pending_reviews) —
// an edit/apply/backfill landing on ANY task bumps that task's version, so
// summing collapses "did anything change" to a single scalar the effect
// below can key on, same loop-guard shape as ReviewTray's totalPending
// (skip-initial-mount ref + disabled exhaustive-deps, since the mount effect
// above already covers first fetch).
export function ActivityTicker() {
  const { tasks } = useTasksStore()
  // getActivity() returns the full merged FeedItem union (events + settled
  // actions, Phase 5), already sorted newest-first server-side — `items` is
  // rendered directly in that order below, narrowing per-item on `type`.
  const [items, setItems] = useState<FeedItem[]>([])
  const actions = useMemo(
    () => items.filter((i): i is ActionFeedItem => i.type === 'action'),
    [items],
  )

  const refetch = useCallback(async () => {
    try {
      const activity = await getActivity()
      setItems(activity)
    } catch (e) {
      console.error('[ActivityTicker] refetch failed', e)
    }
  }, [])

  useEffect(() => { void refetch() }, [refetch])

  const totalVersion = useMemo(
    () => tasks.reduce((sum, t) => sum + t.version, 0),
    [tasks],
  )

  // Cross-task signal for pending-count-only changes: reject actions never
  // bump task.version (server publishes pending_count change only by design),
  // so we need a second signal to catch rejects that are version-exempt. Sum
  // per-task pending_reviews (same shape as ReviewTray's existing signal) so
  // any task's pending count moving is a single scalar to key the effect on.
  const totalPending = useMemo(
    () => tasks.reduce((sum, t) => sum + t.summary.pending_reviews, 0),
    [tasks],
  )

  // Skip the initial mount: the effect above already fetches once on mount,
  // and totalVersion/totalPending's first render is not a "change" worth a second fetch.
  const mountedRef = useRef(false)
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true
      return
    }
    void refetch()
    // Keyed on both totalVersion and totalPending as separate dependencies.
    // totalVersion catches applies/edits/approves (they bump version). totalPending
    // catches rejects (version-exempt by design — see reject route's comment in
    // server/app/api/tasks.py). Separate deps ensure neither cancels the other
    // (if combined as arithmetic sum, approve bumps +1 and drops −1, netting to
    // zero, missing the refetch). Not keyed on `refetch` (stable via useCallback's
    // `[]` deps anyway) — must fire only when the summed signals move.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [totalVersion, totalPending])

  // Third refetch signal, action-only: approve/reject/undo publish
  // `action_updated` directly (app/api/actions.py), never through
  // _publish_task_updated, so neither totalVersion nor totalPending above
  // ever moves for a settled action — an undo elsewhere (another tab, or
  // this task's own TaskActionsPanel) would otherwise never reach this
  // ticker. Pure nudge (task_id only), same idiom as JobsProvider's
  // job_updated handler.
  // Also refetch on _open (SSE reconnect) to catch nudges lost during
  // reconnection, ensuring totalVersion/totalPending proxies stay current.
  useEffect(() => {
    return subscribeSse((e) => {
      if (e.event === '_open' || e.event === 'action_updated') void refetch()
    })
  }, [refetch])

  // Per-action "undo in flight" state, same busy+10s-timeout idiom as every
  // other action button in this codebase (see ReviewTray's approve/reject).
  // Cleared once the NEXT `actions` snapshot no longer has this id at
  // status='executed' — true whether the undo succeeded (status→'undone')
  // or the id simply aged out of the activity window.
  const [undoBusy, setUndoBusy] = useState<Set<string>>(new Set())
  const undoBusyTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  // Per-action inline error from a failed undo (409 needs-permission/missing
  // undo data) — the item itself doesn't disappear on failure (undo only
  // flips status on a REAL Gmail write succeeding), so this is a plain map
  // rather than ReviewTray's remove-or-override dance for a failed approve.
  const [undoErrors, setUndoErrors] = useState<Map<string, string>>(new Map())

  useEffect(() => {
    const stillUndoable = new Set(actions.filter((a) => a.status === 'executed').map((a) => a.action_id))
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
  }, [actions])

  useEffect(() => {
    return () => {
      for (const timer of undoBusyTimersRef.current.values()) {
        clearTimeout(timer)
      }
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

  if (items.length === 0) {
    return (
      <section>
        <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Activity</h2>
        <div style={{ fontSize: 13, color: '#888' }}>No recent activity</div>
      </section>
    )
  }

  return (
    <section>
      <h2 style={{ fontSize: 14, margin: '0 0 8px' }}>Activity</h2>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 4 }}>
        {items.map((item) => {
          if (item.type === 'action') {
            const canUndo = item.status === 'executed' && UNDOABLE_ACTION_TYPES.has(item.action_type)
            return (
              <ActionActivityLine
                key={item.action_id}
                item={item}
                agoText={createdAgo(item.created_at)}
                showTaskLink
                busy={undoBusy.has(item.action_id)}
                undoErrorText={undoErrors.get(item.action_id) ?? null}
                onUndo={canUndo ? () => handleUndo(item) : null}
              />
            )
          }
          return (
            <li
              key={item.id}
              style={{ fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            >
              <Link to={`/tasks/${item.task_id}`} style={{ color: 'inherit', textDecoration: 'none' }}>
                <OriginBadge origin={item.origin} />
                {item.task_name}: {entityLabel(item)} {item.field ?? '—'} → {item.new_value ?? '—'} · {createdAgo(item.created_at)}
              </Link>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
