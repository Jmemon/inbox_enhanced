import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTasksStore } from '../../state/TasksProvider'
import { useInboxStore } from '../../state/InboxProvider'
import {
  approveEvent, attachThread, detachThread, getTaskBoard, getTaskEvents, getTaskThreads,
  rejectEvent, revertEvent, setEntityState,
  type InboxThread, type TaskEntity, type TaskEvent,
} from '../../lib/api'
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

  const refetchEvents = useCallback(() => {
    if (!taskId) return Promise.resolve()
    return getTaskEvents(taskId).then((r) => setEvents(r.events))
      .catch((e) => console.error('[TaskDetail] events fetch failed', e))
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

  const handleMove = useCallback((entityId: string, stage: string) => {
    if (!taskId) return
    void setEntityState(taskId, entityId, 'stage', stage)
      .then(() => Promise.all([refetchBoard(), refetchEvents()]))
      .catch((e) => console.error('[TaskDetail] move failed', e))
  }, [taskId, refetchBoard, refetchEvents])

  const handleOpenEntity = useCallback((entityId: string) => {
    // Task 7 (PipelineBoard + EntityDrawer) owns opening/rendering the
    // entity drawer itself — nothing further to wire from TaskDetail today.
    console.log('[TaskDetail] open entity (drawer lands in Task 7)', entityId)
  }, [])

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

      {/* Main two-column area: board (left, ~2fr) + review feed (right, ~1fr). */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 24, alignItems: 'start' }}>
        <PipelineBoard
          schema={detail.state_schema}
          entities={entities}
          onMove={handleMove}
          onOpenEntity={handleOpenEntity}
        />
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
