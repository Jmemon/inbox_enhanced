import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTasksStore } from '../../state/TasksProvider'
import { useInboxStore } from '../../state/InboxProvider'
import {
  approveEvent, attachThread, detachThread, getTaskBoard, getTaskEvents, getTaskThreads,
  mergeEntity, rejectEvent, revertEvent, setEntityState,
  type InboxThread, type TaskEntity, type TaskEvent,
} from '../../lib/api'
import { EntityDrawer } from './EntityDrawer'
import { PipelineBoard } from './PipelineBoard'
import { ReviewFeed } from './ReviewFeed'
import { ThreadsPanel } from './ThreadsPanel'

// Mirrors InboxList's BucketPill inline-style pattern (and AppShell's
// navStyle) — a tiny status-colored chip, no CSS module needed for one use.
const statusChipStyle = (status: 'active' | 'paused') => ({
  display: 'inline-block', padding: '2px 8px', borderRadius: 999, fontSize: 12, fontWeight: 500,
  background: status === 'active' ? '#e7f1ea' : '#f1eee7',
  color: status === 'active' ? '#2f6b46' : '#8a7a4b',
})

export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const { getDetail, loadDetail, patchTask, deleteTask } = useTasksStore()
  const { buckets } = useInboxStore()

  const [notFound, setNotFound] = useState(false)
  const [entities, setEntities] = useState<TaskEntity[]>([])
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [threads, setThreads] = useState<InboxThread[]>([])
  // Which entity's drawer (Task 7) is open, if any — owned here (not inside
  // PipelineBoard) because the board's props are locked to
  // {schema, entities, onMove, onOpenEntity} and have nowhere to hang drawer
  // events off of; TaskDetail renders EntityDrawer as PipelineBoard's sibling
  // instead, fed from state it already owns (entitiesById, events, schema).
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)

  // TasksProvider's cache, keyed by id — this is what the refetch effect
  // below watches for identity changes (see its comment).
  const detail = taskId ? getDetail(taskId) : undefined

  // Initial detail load. loadDetail populates TasksProvider's cache (so
  // `detail` above becomes defined) and returns null on 404 — render "task
  // not found" instead of an empty shell in that case. Also resets the
  // locally-owned board/events/threads state so a param change (navigating
  // straight from one task's page to another's, without AppShell unmounting
  // this route) doesn't briefly show the previous task's data.
  useEffect(() => {
    if (!taskId) return
    setNotFound(false)
    setEntities([])
    setEvents([])
    setThreads([])
    // Also drop any selected entity — otherwise navigating straight from one
    // task's page to another's (route param change, no unmount) could leave
    // a stale selectedEntityId pointing at an id from the PREVIOUS task,
    // opening a drawer for an entity that isn't even this task's.
    setSelectedEntityId(null)
    void loadDetail(taskId).then((d) => { if (d === null) setNotFound(true) })
  }, [taskId, loadDetail])

  // Board/events/threads are owned HERE, not the provider (unlike the task
  // list/detail themselves) — each is its own GET, refetched independently
  // by the action handlers below and all three refetched together whenever
  // the cached detail's identity changes.
  const refetchBoard = useCallback(() => {
    if (!taskId) return Promise.resolve()
    return getTaskBoard(taskId).then((r) => setEntities(r.entities))
      .catch((e) => console.error('[TaskDetail] board fetch failed', e))
  }, [taskId])

  // Two parallel windows, merged: the recent-events window (newest 50, any
  // status, as before) AND a wider pending-only window (newest 200
  // pending_review events). Why: task.summary.pending_reviews (the review
  // tray's badge count) is a live server-side COUNT with no window limit,
  // but the tray itself only ever rendered whatever pending events happened
  // to fall inside the recent-50 fetch — a pending event older than that
  // window (e.g. a backfill run queuing many extractions at once) would be
  // counted in the badge yet permanently invisible in the tray. Merge by id
  // (pendings ∪ recent, deduped — the same event can appear in both windows)
  // then re-sort newest-first, since list_task_events is itself newest-first
  // per call but a straight concat of two independently-paged windows
  // wouldn't preserve that across the merge. ReviewFeed needs no change: it
  // just filters this same `events` state by status.
  const refetchEvents = useCallback(() => {
    if (!taskId) return Promise.resolve()
    return Promise.all([
      getTaskEvents(taskId),
      getTaskEvents(taskId, { status: 'pending_review', limit: 200 }),
    ]).then(([recent, pending]) => {
      const byId = new Map<string, TaskEvent>()
      for (const e of recent.events) byId.set(e.id, e)
      for (const e of pending.events) byId.set(e.id, e)
      const merged = Array.from(byId.values())
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
      setEvents(merged)
    }).catch((e) => console.error('[TaskDetail] events fetch failed', e))
  }, [taskId])

  const refetchThreads = useCallback(() => {
    if (!taskId) return Promise.resolve()
    return getTaskThreads(taskId).then((r) => setThreads(r.threads))
      .catch((e) => console.error('[TaskDetail] threads fetch failed', e))
  }, [taskId])

  const refetchAll = useCallback(() => {
    void refetchBoard()
    void refetchEvents()
    void refetchThreads()
  }, [refetchBoard, refetchEvents, refetchThreads])

  // SSE-driven refetch cascade: TasksProvider already refetches its cached
  // `detail` when a task_updated event bumps version OR moves
  // pending_reviews (its 2A-mandated no-bump-on-reject/attach/detach rule —
  // see TasksProvider.tsx's task_updated handler comment). This effect keys
  // on those exact two fields (not `detail` itself, which is a fresh object
  // on every loadDetail call and would refire every render) so that whenever
  // the provider's refetch actually changed something observable, this page
  // cascades into refetching its own board/events/threads.
  useEffect(() => {
    if (!detail) return
    refetchAll()
    // Deps intentionally limited to the two fields that signal a real
    // change (see comment above) — refetchAll is stable per taskId and
    // re-running this effect on its identity would defeat the point.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.version, detail?.summary.pending_reviews])

  const entitiesById = useMemo(() => {
    const m: Record<string, TaskEntity> = {}
    for (const e of entities) m[e.id] = e
    return m
  }, [entities])

  // GET /api/tasks/{id}/threads is unordered — sort newest-first client-side.
  const sortedThreads = useMemo(
    () => threads.slice().sort(
      (a, b) => (b.recent_message?.internal_date ?? 0) - (a.recent_message?.internal_date ?? 0),
    ),
    [threads],
  )

  // Generalizes the old handleMove's hardcoded 'stage' field: the drawer's
  // per-attribute inline edits share this same setEntityState-then-refetch
  // round trip for ANY field. Async and RE-THROWS (unlike this file's other
  // handlers below, which swallow failures into console.error) because
  // EntityDrawer awaits this promise itself and renders the rejection's
  // message inline — the schema validator's 422 detail is meaningful there
  // (e.g. "'x' is not a valid stage"), unlike a bare console.error.
  const handleEditEntity = useCallback(async (entityId: string, field: string, value: string) => {
    if (!taskId) return
    await setEntityState(taskId, entityId, field, value)
    await Promise.all([refetchBoard(), refetchEvents()])
  }, [taskId, refetchBoard, refetchEvents])

  // PipelineBoard's per-card "move to" select still wants a synchronous,
  // fire-and-forget callback (its locked onMove prop shape) — just the
  // 'stage' case of handleEditEntity above, with failures logged instead of
  // propagated (nothing in the board is positioned to render an inline
  // error for it, unlike the drawer).
  const handleMove = useCallback((entityId: string, stage: string) => {
    void handleEditEntity(entityId, 'stage', stage)
      .catch((e) => console.error('[TaskDetail] move failed', e))
  }, [handleEditEntity])

  // window.confirm lives HERE (not inside EntityDrawer) — the drawer just
  // calls onMerge for whatever target the user picked; TaskDetail owns the
  // confirmation, the API call, closing the drawer, and the refetch.
  const handleMergeEntity = useCallback((entityId: string, intoEntityId: string) => {
    if (!taskId) return
    const loserName = entitiesById[entityId]?.display_name ?? entityId
    const winnerName = entitiesById[intoEntityId]?.display_name ?? intoEntityId
    if (!window.confirm(`Merge "${loserName}" into "${winnerName}"? This cannot be undone.`)) return
    void mergeEntity(taskId, entityId, intoEntityId)
      .then(() => {
        setSelectedEntityId(null)
        return Promise.all([refetchBoard(), refetchEvents()])
      })
      .catch((e) => console.error('[TaskDetail] merge failed', e))
  }, [taskId, entitiesById, refetchBoard, refetchEvents])

  const handleApprove = useCallback((eventId: string) => {
    if (!taskId) return
    void approveEvent(taskId, eventId)
      .then(() => Promise.all([refetchEvents(), refetchBoard()]))
      .catch((e) => console.error('[TaskDetail] approve failed', e))
  }, [taskId, refetchEvents, refetchBoard])

  const handleReject = useCallback((eventId: string) => {
    if (!taskId) return
    void rejectEvent(taskId, eventId)
      .then(() => Promise.all([refetchEvents(), refetchBoard()]))
      .catch((e) => console.error('[TaskDetail] reject failed', e))
  }, [taskId, refetchEvents, refetchBoard])

  const handleRevert = useCallback((eventId: string) => {
    if (!taskId) return
    void revertEvent(taskId, eventId)
      .then(() => Promise.all([refetchEvents(), refetchBoard()]))
      .catch((e) => console.error('[TaskDetail] revert failed', e))
  }, [taskId, refetchEvents, refetchBoard])

  const handleDetach = useCallback((threadId: string, addExample: boolean) => {
    if (!taskId) return
    void detachThread(taskId, threadId, addExample)
      .then(() => refetchAll())
      .catch((e) => console.error('[TaskDetail] detach failed', e))
  }, [taskId, refetchAll])

  const handleAttach = useCallback((threadId: string, addExample: boolean) => {
    if (!taskId) return
    void attachThread(taskId, threadId, addExample)
      .then(() => Promise.all([refetchThreads(), refetchEvents()]))
      .catch((e) => console.error('[TaskDetail] attach failed', e))
  }, [taskId, refetchThreads, refetchEvents])

  const handleToggleStatus = useCallback(() => {
    if (!taskId || !detail) return
    void patchTask(taskId, { status: detail.status === 'active' ? 'paused' : 'active' })
      .catch((e) => console.error('[TaskDetail] status toggle failed', e))
  }, [taskId, detail, patchTask])

  const handleDelete = useCallback(() => {
    if (!taskId || !detail) return
    if (!window.confirm(`Delete task "${detail.name}"? This cannot be undone.`)) return
    void deleteTask(taskId)
      .then(() => navigate('/'))
      .catch((e) => console.error('[TaskDetail] delete failed', e))
  }, [taskId, detail, deleteTask, navigate])

  if (!taskId) return null

  if (notFound) {
    return (
      <main style={{ padding: '32px 24px' }}>
        <div style={{ fontSize: 16, marginBottom: 8 }}>task not found</div>
        <Link to="/" style={{ fontSize: 13 }}>← back to HUD</Link>
      </main>
    )
  }

  if (!detail) {
    return <main style={{ padding: '32px 24px', color: '#888' }}>loading…</main>
  }

  // Plain consts (not hooks) computed after the early returns above — safe
  // since they aren't hooks and don't need to run unconditionally. `schema`
  // narrows detail.state_schema to non-null once for both the board and the
  // drawer below; `selectedEntity` narrows selectedEntityId → an actual
  // TaskEntity (or undefined if it no longer exists, e.g. right after a
  // merge folded it away before the drawer's own onClose ran).
  const schema = detail.state_schema
  const selectedEntity = selectedEntityId ? entitiesById[selectedEntityId] : undefined

  return (
    <main style={{ padding: '16px 24px', display: 'grid', gap: 24 }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h1 style={{ fontSize: 20, margin: 0 }}>{detail.name}</h1>
        <span style={statusChipStyle(detail.status)}>{detail.status}</span>
        <button onClick={handleToggleStatus} style={{ fontSize: 13, padding: '6px 10px' }}>
          {detail.status === 'active' ? 'pause' : 'resume'}
        </button>
        <button
          onClick={handleDelete}
          style={{ fontSize: 13, padding: '6px 10px', marginLeft: 'auto', color: '#8a1c25' }}
        >
          delete
        </button>
      </header>

      {/* Main two-column area: board (left, ~2fr) + review feed (right, ~1fr).
          EntityDrawer renders as PipelineBoard's SIBLING here (not nested
          inside it — the board's locked props have nowhere to hang drawer
          events off of); it's a fixed-position overlay so its place in this
          grid doesn't affect layout either way. */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 24, alignItems: 'start' }}>
        <PipelineBoard
          schema={schema}
          entities={entities}
          onMove={handleMove}
          onOpenEntity={setSelectedEntityId}
        />
        {selectedEntity && schema && (
          <EntityDrawer
            taskId={taskId}
            entity={selectedEntity}
            schema={schema}
            events={events}
            onClose={() => setSelectedEntityId(null)}
            onEdit={(field, value) => handleEditEntity(selectedEntity.id, field, value)}
            onMerge={(intoId) => handleMergeEntity(selectedEntity.id, intoId)}
            onRevert={handleRevert}
            allEntities={entities}
          />
        )}
        <ReviewFeed
          events={events}
          entitiesById={entitiesById}
          onApprove={handleApprove}
          onReject={handleReject}
          onRevert={handleRevert}
        />
      </div>

      <ThreadsPanel
        taskId={taskId}
        threads={sortedThreads}
        bucketsById={buckets.byId}
        onDetach={handleDetach}
        onAttach={handleAttach}
      />
    </main>
  )
}
